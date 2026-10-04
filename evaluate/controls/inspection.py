"""Internal read-only projections. No public route and no raw ORM/session dump."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from uuid import uuid4

from django.contrib.sessions.models import Session
from django.core.exceptions import ValidationError
from evaluate.contracts.interfaces import Blocked
from evaluate.contracts.models import StateSnapshot
from evaluate.evidence.redaction import redact_value, assert_redacted
from .ownership import owned


def project(value, fields):
    return {k: value[k] for k in fields if k in value}


_UNDECIDED = object()
# application.v1 checker contract keys (keep legacy aliases when source data exists):
# basket.items[]: item_id, variant_id (+ item_variant_id), quantity (int), unit_price_minor (int)
# basket: subtotal_minor, fee_minor, tax_minor, discount_minor, total_minor, currency
# quote: id, valid (bool)
# orders/payments/addresses/commands/effects: tenant_id and/or customer_id when owned
# payments: amount_minor (+ requested_minor/captured_minor/refunded_minor)
# effects: operation_id, kind (from command dedupe_key / kind)
# pos: status, optional order_id


def pending_question(data):
    """Open follow-up text, None when the queue says nothing is open, or undecided."""
    index = data.get('awaiting_followup_index')
    queue = data.get('ongoing_query_queue')
    if not isinstance(queue, list) or any(not isinstance(item, dict) for item in queue):
        return _UNDECIDED
    if index is None or not queue:
        return None
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(queue):
        return _UNDECIDED
    item = queue[index]
    if not isinstance(item, dict):
        return _UNDECIDED
    questions = item.get('follow_up_question')
    if isinstance(questions, str):
        return questions or None
    if isinstance(questions, list):
        texts = [question for question in questions if isinstance(question, str) and question.strip()]
        return texts[-1] if texts else _UNDECIDED
    return _UNDECIDED


def safe(value):
    value = redact_value(value)
    if isinstance(value, str):
        return re.sub(r'https?://\S+', '[REDACTED:url]', value)
    if isinstance(value, dict):
        return {k: safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [safe(v) for v in value]
    return value


def as_int(value):
    """Return an int when value is already an int or a whole-number string/Decimal."""
    if type(value) is int:
        return value
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number != number.to_integral_value():
        return None
    return int(number)


def as_minor(value, exponent=2):
    """Convert major-unit money to integer minor units; None when absent or malformed."""
    if value is None or value == '':
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return int((number * (10 ** exponent)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def project_basket_item(item):
    row = project(item, ('item_id', 'item_variant_id', 'name', 'size', 'quantity', 'price',
                         'unit_price', 'item_number', 'modifiers', 'special_instructions'))
    variant = item.get('item_variant_id', item.get('variant_id'))
    if variant is not None:
        row['variant_id'] = variant
        row.setdefault('item_variant_id', variant)
    quantity = as_int(item.get('quantity'))
    if quantity is not None:
        row['quantity'] = quantity
    unit_minor = item.get('unit_price_minor')
    if type(unit_minor) is not int:
        unit_minor = as_minor(item.get('unit_price', item.get('price')))
    if type(unit_minor) is int:
        row['unit_price_minor'] = unit_minor
    return row


def project_quote(checkout):
    """Emit quote.id / quote.valid for a live quote or an evaluation-invalidated one."""
    quote = checkout.get('quote')
    if isinstance(quote, dict) and quote:
        identity = quote.get('id', quote.get('fingerprint'))
        if identity is None:
            return None
        valid = quote.get('valid')
        if type(valid) is not bool:
            valid = True
        projection = {'id': str(identity), 'valid': valid}
    else:
        invalidated = checkout.get('invalidated_quote')
        if not isinstance(invalidated, dict) or invalidated.get('id') is None:
            return None
        if invalidated.get('valid') is not False:
            return None
        projection = {'id': str(invalidated['id']), 'valid': False}
    if isinstance(quote, dict):
        for key in ('mode', 'payment_method', 'currency', 'fingerprint'):
            if key in quote and key not in projection:
                projection[key] = quote[key]
    return projection


def apply_quote_money(basket, quote):
    """Attach integer minor-unit basket totals when the quote (or commerce block) has them."""
    if not isinstance(quote, dict):
        return
    commerce = quote.get('commerce') if isinstance(quote.get('commerce'), dict) else None
    exponent = 2
    if commerce and type(commerce.get('exponent')) is int:
        exponent = commerce['exponent']
    mapping = {}
    if commerce:
        for src, dest in (('subtotal_minor', 'subtotal_minor'), ('tax_minor', 'tax_minor'),
                          ('discount_minor', 'discount_minor'), ('total_minor', 'total_minor')):
            if type(commerce.get(src)) is int:
                mapping[dest] = commerce[src]
        fees = commerce.get('fees')
        if isinstance(fees, list) and fees and all(type(f.get('subtotal_minor')) is int for f in fees):
            mapping['fee_minor'] = sum(f['subtotal_minor'] for f in fees)
        if commerce.get('currency') is not None:
            mapping['currency'] = commerce['currency']
    for src, dest in (('subtotal', 'subtotal_minor'), ('fee', 'fee_minor'), ('tax', 'tax_minor'),
                      ('discount', 'discount_minor'), ('total', 'total_minor')):
        if dest not in mapping:
            converted = as_minor(quote.get(src), exponent)
            if converted is not None:
                mapping[dest] = converted
    if 'currency' not in mapping and quote.get('currency') is not None:
        mapping['currency'] = quote['currency']
    _imply_missing_discount(mapping)
    basket.update(mapping)


def _imply_missing_discount(mapping):
    """A complete quote with no discount component has a zero discount.

    Do not invent a component when the other money fields are themselves missing.
    """
    required = ('subtotal_minor', 'fee_minor', 'tax_minor', 'total_minor')
    if 'discount_minor' in mapping or any(name not in mapping for name in required):
        return
    implied = mapping['subtotal_minor'] + mapping['fee_minor'] + mapping['tax_minor'] - mapping['total_minor']
    if implied >= 0:
        mapping['discount_minor'] = implied


def apply_line_unit_prices(items, quote):
    """Copy integer unit prices from a commerce quote onto basket lines that lack them."""
    if not isinstance(quote, dict):
        return
    commerce = quote.get('commerce') if isinstance(quote.get('commerce'), dict) else None
    if not commerce or not isinstance(commerce.get('lines'), list):
        return
    by_variant = {}
    for line in commerce['lines']:
        if not isinstance(line, dict) or type(line.get('unit_minor')) is not int:
            continue
        for key in ('variant_id', 'item_variant_id'):
            if line.get(key) is not None:
                by_variant[str(line[key])] = line['unit_minor']
    for item in items:
        if type(item.get('unit_price_minor')) is int:
            continue
        variant = item.get('variant_id', item.get('item_variant_id'))
        if variant is not None and str(variant) in by_variant:
            item['unit_price_minor'] = by_variant[str(variant)]


class StateInspector:
    def __init__(self, provisioner):
        self.provisioner = provisioner

    def snapshot(self, lease, identity, original_turn_index, request_id, phase):
        from orders import models as om
        from commerce import models as cm
        tenant, owner = owned(lease, identity, self.provisioner)
        customer = owner['maps']['customer:active']
        tenant_id, customer_id = str(tenant.pk), str(customer)
        sessions = om.ChatSession.objects.filter(tenant=tenant, customer_id=customer,
            platform='website', session_id=owner['browser_sessions'][0]).order_by('-created_at')
        chat = sessions.first()
        browser = Session.objects.filter(session_key=owner['browser_sessions'][0]).first()
        state, unavailable = {'projection_version': 'application.v1',
            'ownership': {'tenant_id': tenant_id, 'customer_id': customer_id},
            'message_bindings': owner.get('message_bindings', {})}, []
        if browser is None:
            unavailable.extend(['chat', 'basket', 'address_selection'])
        else:
            data = browser.get_decoded().get(owner['namespace'], {})
            if str(data.get('customer_id')) != str(customer):
                raise Blocked('Browser state does not belong to the owned customer')
            state['chat'] = dict(id=str(chat.pk), completed=chat.is_completed,
                **project(data, ('awaiting_followup_index',)))
            queue = data.get('ongoing_query_queue')
            if isinstance(queue, list) and all(isinstance(q, dict) for q in queue):
                state['chat']['ongoing_query_queue'] = [project(q, (
                    'query_id', 'intent_type', 'sub_intent', 'is_complete', 'follow_up_question',
                    'main_intent', 'completed')) for q in queue]
            # Absent/malformed queues are unknown, not proof of no pending work.
            question = pending_question(data)
            if question is not _UNDECIDED:
                state['chat']['pending_question'] = question
            basket = data.get('basket')
            items = basket.get('items') if isinstance(basket, dict) else None
            if isinstance(items, list) and all(isinstance(item, dict) for item in items):
                state['basket'] = {'items': [project_basket_item(item) for item in items]}
            else:
                unavailable.append('basket')
            state['address_selection'] = project(data.get('delivery_address', {}),
                ('address_id', 'postal_code', 'is_verified'))
            state['address_selection']['confirmed'] = bool(data.get('checklist', {}).get('location', False))
            selected_address = state['address_selection'].get('address_id')
            try:
                authorized = (not selected_address or om.CustomerAddress.objects.filter(
                    pk=selected_address, tenant=tenant, customer_id=customer).exists())
            except (ValueError, TypeError, ValidationError):
                authorized = False
            state['address_selection']['authorized'] = bool(authorized)
        state['addresses'] = [dict(id=str(a.pk), label=a.label, is_default=a.is_default,
            tenant_id=tenant_id, customer_id=customer_id,
            components=project(a.components or {}, ('street_address', 'house_or_flat', 'building_or_block', 'street_or_locality', 'sector_or_phase', 'landmark',
                'city', 'state', 'postal_code', 'country')))
            for a in om.CustomerAddress.objects.filter(tenant=tenant, customer_id=customer)]
        checkout = (chat.state or {}).get('checkout', {})
        state['checkout'] = project(checkout, ('mode', 'payment_method', 'status', 'scheduled_at'))
        state['checkout']['fields'] = project(checkout.get('fields', {}), ('scheduled_at', 'postal_code', 'table_id'))
        quote = project_quote(checkout)
        if quote is not None:
            state['quote'] = quote
        if 'basket' in state:
            live_quote = checkout.get('quote') if isinstance(checkout.get('quote'), dict) else None
            if live_quote is not None:
                apply_line_unit_prices(state['basket']['items'], live_quote)
            apply_quote_money(state['basket'], live_quote)
        state['orders'] = [dict(id=str(o.pk), status=o.order_status, payment_status=o.payment_status,
            total=str(o.total_amount), total_minor=as_minor(o.total_amount), payment_mode=o.payment_mode,
            mode=(o.meta or {}).get('checkout', {}).get('mode'),
            scheduled_at=(o.meta or {}).get('checkout', {}).get('scheduled_at'),
            fields=project((o.meta or {}).get('checkout', {}).get('fields', {}), ('table_id', 'postal_code')),
            tenant_id=str(o.tenant_id), customer_id=str(o.customer_id))
            for o in om.Order.objects.filter(tenant=tenant, customer_id=customer)]
        records = cm.AcceptedOrder.objects.filter(order__tenant=tenant, order__customer_id=customer,
                                                  location__tenant=tenant)
        state['payments'] = [dict(id=str(p.pk), order_id=str(p.accepted_order.order_id),
            status=p.status, currency=p.currency, requested_minor=p.requested_minor,
            captured_minor=p.captured_minor, refunded_minor=p.refunded_minor,
            amount_minor=p.requested_minor, provider_created=bool(p.external_id),
            tenant_id=tenant_id, customer_id=customer_id)
            for p in cm.Payment.objects.filter(accepted_order__in=records, connection__location__tenant=tenant)
                .select_related('accepted_order')]
        commands = list(cm.Command.objects.filter(accepted_order__in=records, connection__location__tenant=tenant)
                        .select_related('accepted_order'))
        state['commands'] = [dict(id=str(c.pk), order_id=str(c.accepted_order.order_id),
            kind=c.kind, status=c.status, attempts=c.attempts,
            tenant_id=tenant_id, customer_id=customer_id) for c in commands]
        state['effects'] = [dict(id=str(c.pk), operation_id=c.dedupe_key, kind=c.kind,
            order_id=str(c.accepted_order.order_id), status=c.status,
            tenant_id=tenant_id, customer_id=customer_id) for c in commands]
        state['reconciliation'] = [dict(id=str(r.pk), code=r.code, resolved=r.resolved_at is not None,
            order_id=str(r.accepted_order.order_id))
            for r in cm.ReconciliationIssue.objects.filter(accepted_order__in=records).select_related('accepted_order')]
        active = records.filter(order_id=chat.order_id).first() if chat.order_id else None
        payment = next((p for p in state['payments'] if p['order_id'] == str(chat.order_id)), None)
        state['payment'] = payment or {'status': 'not_requested'}
        if active:
            state['pos'] = {'order_id': str(chat.order_id), 'status': active.pos_state,
                            'tenant_id': tenant_id, 'customer_id': customer_id}
        else:
            state['pos'] = {'status': 'not_requested'}
        state['pending_async'] = (['payment'] if payment and payment['status'] == 'pending' else [])
        if active and active.pos_state == 'pending':
            state['pending_async'].append('pos')
        # External provider receipts require the provider adapter, not these ORM rows.
        unavailable.append('provider_receipts')
        state = safe(state)
        assert_redacted(state)
        return StateSnapshot(**identity.model_dump(), event_id=str(uuid4()), snapshot_id=str(uuid4()),
            original_turn_index=original_turn_index, request_id=request_id, phase=phase,
            captured_at=datetime.now(timezone.utc).isoformat(), state=state, unavailable_sections=unavailable)
