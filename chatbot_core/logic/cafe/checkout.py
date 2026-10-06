"""Durable checkout drafts. Session locks serialize confirmation and recovery.

Quotes are disposable; an order is created only after an explicit confirmation
of the current policy, basket, fulfillment fields and price.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
from zoneinfo import ZoneInfo

from django.db import transaction
from django.utils import timezone
from orders.checkout_config import CheckoutPolicy
from orders.models import ChatSession, Order
from .catalog import load_catalog, validate_selection, selection_prices_match, selection_unit_total
from .db_utils import create_order

MODE_LABELS = {'delivery': 'delivery', 'pickup': 'pickup', 'dine_in': 'dine-in'}
CONFIRM = re.compile(r'(?:confirm|confirm order|place order)[.!]?', re.I)
START_CHECKOUT = re.compile(r'(?:checkout|check out|resume checkout|confirm(?: order)?|place order|pay(?: now| for (?:my |the )?order)?)[.!]?', re.I)
MODE_REQUEST = re.compile(
    r'(?:(?:switch(?:\s+to)?|change(?:\s+(?:it|this|my order))?\s+to|i(?:\s+will|\x27ll|\s+want(?:\s+to)?)|please|for)\s+)?'
    r'(delivery|deliver(?:\s+it)?|pickup|pick\s+up|takeaway|dine[ -]?in)(?:\s+(?:please|instead))?[.!]?', re.I)


def drop_quote(draft):
    """Remove the current quote. Evaluation runs keep its id so invalidation is checkable."""
    previous = draft.pop('quote', None)
    remember_invalidated_quote(draft, previous)
    return previous


def remember_invalidated_quote(draft, previous):
    """Stash quote identity only while an evaluation request is active."""
    from evaluate.controls.context import current
    if current() is None or not isinstance(previous, dict):
        return
    identity = previous.get('id', previous.get('fingerprint'))
    if identity is None:
        return
    draft['invalidated_quote'] = {'id': identity, 'valid': False}


PROMPTS = {
    'name': 'What recepient name should we use for this order?',
    'phone': 'What phone number should we use for this order?',
    'address': 'What is your delivery address? Reply with address: followed by the full address.',
    'postal_code': 'What is your delivery postal code?',
    'table_id': 'What is your table identifier?',
    'scheduled_at': 'What pickup time would you like? Use YYYY-MM-DD HH:MM in the café timezone.',
}


def mode_request(text):
    match = MODE_REQUEST.fullmatch(text.strip())
    if not match:
        return None
    value = match[1].lower()
    return 'delivery' if value.startswith('deliver') else 'dine_in' if value.startswith('dine') else 'pickup'


def remember_fulfillment_preference(checklist, value, configuration):
    """Selecting a mode before checkout records a preference without opening a draft."""
    if value not in MODE_LABELS:
        raise ValueError('Please choose delivery, pickup or dine-in.')
    if configuration is not None:
        policy = CheckoutPolicy.model_validate(configuration)
        if value not in policy.modes:
            raise ValueError('That fulfillment mode is not available at this café.')
    checklist['fulfillment_preference'] = value
    return MODE_LABELS[value]


def basket_total(basket, tenant):
    from commerce.menu_sync import assert_menu_fresh
    assert_menu_fresh(tenant.pk)
    if basket.is_empty():
        raise ValueError('Your basket is empty. Add an item before checkout.')
    catalog = load_catalog(tenant.api_key)
    total = Decimal('0')
    for entry in basket.items:
        selection = validate_selection(catalog, entry['item_id'], entry['item_variant_id'],
                                       entry['quantity'], entry.get('modifiers', []))
        if not selection_prices_match(entry, selection):
            raise ValueError('A menu price changed. Please update your basket before checkout.')
        total += selection_unit_total(selection) * selection['quantity']
    return total.quantize(Decimal('0.01'))


def service_time(policy, mode_policy, fields, now):
    zone = ZoneInfo(policy.timezone)
    earliest = now + timedelta(minutes=mode_policy.preparation_minutes)
    requested = fields.get('scheduled_at')
    if requested:
        try:
            when = datetime.fromisoformat(requested)
            if when.tzinfo is None:
                # Reject nonexistent and ambiguous wall times instead of silently
                # choosing the wrong UTC instant around daylight-saving changes.
                local = when.replace(tzinfo=zone)
                if (local.astimezone(ZoneInfo('UTC')).astimezone(zone).replace(tzinfo=None) != when
                        or local.utcoffset() != local.replace(fold=1).utcoffset()):
                    raise ValueError()
                when = local
            when = when.astimezone(zone)
        except (ValueError, TypeError):
            raise ValueError('Please give an unambiguous time using YYYY-MM-DD HH:MM, with a UTC offset if needed.')
        if not mode_policy.scheduling_enabled:
            raise ValueError('Scheduling is unavailable for this mode. Say "as soon as possible" to clear the time.')
        if when < earliest or when > now + timedelta(days=mode_policy.max_advance_days):
            raise ValueError(f'Choose a time at least {mode_policy.preparation_minutes} minutes from now and within {mode_policy.max_advance_days} days.')
    else:
        when = earliest.astimezone(zone)
    if policy.opening_hours:
        def is_open(moment):
            return any(start <= moment.strftime('%H:%M') < end
                       for start, end in policy.opening_hours.get(str(moment.weekday()), []))
        # Immediate orders need the kitchen open now and at fulfillment time.
        if not is_open(when) or (not requested and not is_open(now.astimezone(zone))):
            raise ValueError('The café is closed for that time. Please choose a time during opening hours.'
                             if mode_policy.scheduling_enabled else 'The café is closed for immediate orders. Please try during opening hours.')
    return when


def _checkout_now(tenant, now=None):
    if now is not None:
        return now
    from evaluate.controls.context import business_now, current
    return business_now(tenant.pk) if current() else timezone.now()


def _validate_postal_code(policy, mode, fields):
    postal_code = fields.get('postal_code')
    if (mode == 'delivery' and policy.delivery_postal_codes and postal_code
            and postal_code.upper() not in policy.delivery_postal_codes):
        raise ValueError('That postal code is outside our delivery area. Enter another postal code or switch to pickup.')


def _validate_checkout_field(policy, draft, field, now):
    """Validate one staged field with the same rules used by a full quote."""
    mode = draft.get('mode')
    fields = draft.setdefault('fields', {})
    if field == 'scheduled_at' and fields.get(field):
        service_time(policy, policy.modes[mode], fields, now)
    elif field == 'postal_code' and fields.get(field):
        _validate_postal_code(policy, mode, fields)


def quote(policy, draft, basket, tenant, now=None):
    mode = draft.get('mode')
    if mode not in policy.modes:
        return None, 'mode', 'Choose a fulfillment mode: ' + ', '.join(MODE_LABELS[m] for m in policy.modes) + '.'
    rules = policy.modes[mode]
    fields = draft.setdefault('fields', {})
    effective_now = _checkout_now(tenant, now)
    scheduled_when = None
    if fields.get('scheduled_at'):
        scheduled_when = service_time(policy, rules, fields, effective_now)
    try:
        _validate_postal_code(policy, mode, fields)
    except ValueError as exc:
        return None, 'postal_code', str(exc)
    required = list(rules.required_fields)
    if mode == 'delivery' and policy.delivery_postal_codes and 'postal_code' not in required:
        required.append('postal_code')
    for field in required:
        if field == 'address' and draft.get('address_components'):
            from .location_utils import get_missing_address_keys
            missing = get_missing_address_keys(draft['address_components'])
            if missing:
                return None, 'postal_code' if missing == ['postal_code'] else 'address', address_question(missing)
        if not fields.get(field):
            return None, field, PROMPTS[field]
    payment = draft.get('payment_method')
    if payment not in rules.payment_methods:
        if len(rules.payment_methods) == 1:
            payment = draft['payment_method'] = rules.payment_methods[0]
        else:
            return None, 'payment_method', 'Choose a payment method: ' + ', '.join(rules.payment_methods) + '.'
    subtotal = basket_total(basket, tenant)
    when = scheduled_when or service_time(policy, rules, fields, effective_now)
    details = {'mode': mode, 'fields': deepcopy(fields), 'payment_method': payment,
               'subtotal': str(subtotal), 'fee': str(rules.fee), 'total': str(subtotal + rules.fee),
               'scheduled_at': when.isoformat() if fields.get('scheduled_at') else None,
               'preparation_minutes': rules.preparation_minutes, 'timezone': policy.timezone}
    from commerce.services import basket_quote
    from commerce.pricing import major, minor
    commerce_price = basket_quote(tenant, basket, mode=mode, fee=rules.fee,
                                  discount_code=draft.get('discount_code', ''))
    if payment == 'online':
        from orders.checkout_config import online_provider_ready
        if not online_provider_ready(policy.online_provider, tenant):
            raise ValueError('Online checkout is unavailable until an external payment adapter is configured.')
    if commerce_price:
        details['commerce'] = commerce_price
        details['subtotal'] = str(major(commerce_price['subtotal_minor'], commerce_price['exponent']))
        fees = {f['code']: major(f['subtotal_minor'], commerce_price['exponent']) for f in commerce_price['fees']}
        details['fulfillment_fee'] = str(fees.get('fulfillment', 0))
        details['packaging_fee'] = str(fees.get('packaging', 0))
        details['fee'] = str(sum(fees.values(), Decimal(0)))
        details['total'] = str(major(commerce_price['total_minor'], commerce_price['exponent']))
    else:
        from orders.pricing import catalog_taxes
        tax, details['line_taxes'] = catalog_taxes(tenant, basket.items)
        details['tax'] = str(tax)
        details['total'] = str(subtotal + rules.fee + tax)
        details['fulfillment_fee'] = str(rules.fee)
        details['packaging_fee'] = '0'
    from .ordering_limits import assert_checkout, load_policy
    ordering_policy = load_policy(tenant_id=tenant.pk)
    if commerce_price:
        if (ordering_policy is None or commerce_price['currency'] != ordering_policy.currency
                or commerce_price['exponent'] != ordering_policy.exponent):
            raise ValueError('Checkout currency does not match the ordering policy.')
        payable_minor = commerce_price['total_minor']
    elif ordering_policy is None:
        raise ValueError('Ordering is unavailable until quantity and amount limits are configured.')
    else:
        payable_minor = minor(Decimal(details['total']), ordering_policy.exponent)
    assert_checkout(basket.items, tenant, payable_minor)
    if payment == 'online' and Decimal(details['total']) <= 0:
        raise ValueError('Online checkout requires a positive total. Choose cash for a zero-total order.')
    minimum = major(minor(rules.minimum_order, commerce_price['exponent']), commerce_price['exponent']) if commerce_price else rules.minimum_order
    if Decimal(details['subtotal']) < minimum:
        raise ValueError(f'The minimum order for {MODE_LABELS[mode]} is {minimum}. Your basket is {details["subtotal"]}.')
    fingerprint = hashlib.sha256(json.dumps({
        'quote': details, 'basket': basket.to_dict(),
        'policy': policy.model_dump(mode='json'),
        'ordering_policy': ordering_policy.model_dump(mode='json'),
    }, sort_keys=True).encode()).hexdigest()
    return {**details, 'fingerprint': fingerprint}, None, None


def _update(draft, text, policy):
    if text.lower() in ('checkout', 'check out', 'resume checkout'):
        return None
    mode = mode_request(text)
    if mode:
        _set_mode(draft, mode, policy)
        return 'mode'
    if text.lower().startswith('discount '):
        draft['discount_code'] = text[9:].strip()
        drop_quote(draft)
        return 'discount_code'
    if text.lower() == 'remove discount':
        draft.pop('discount_code', None)
        drop_quote(draft)
        return 'discount_code'
    if text.lower() in ('cash', 'online'):
        _set_payment(draft, text.lower(), policy)
        return 'payment_method'
    if text.lower() in ('asap', 'as soon as possible'):
        draft.setdefault('fields', {}).pop('scheduled_at', None)
        return 'scheduled_at'
    scheduled = re.fullmatch(r'(?:schedule(?:\s+(?:for|at))?\s+|pickup at\s+)?(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?)', text, re.I)
    if scheduled:
        _set_field(draft, 'scheduled_at', scheduled[1])
        return 'scheduled_at'
    explicit = re.fullmatch(r'(name|phone|address|postal code|table(?: id)?):\s*(.+)', text, re.I)
    # The classifier supplies a labelled value for every checkout field. Never
    # persist a contextual sentence just because this slot is awaiting a reply.
    if explicit:
        field = {'postal code': 'postal_code', 'table': 'table_id', 'table id': 'table_id'}.get(explicit[1].lower(), explicit[1].lower())
        text = explicit[2].strip()
        if text.casefold().strip(' .!?') in {'hmm', 'ok', 'okay', 'yes', 'no', 'pay', 'proceed', 'sure'}:
            return None
        _set_field(draft, field, text)
        return field
    return None


def _set_mode(draft, mode, policy):
    if mode not in policy.modes:
        raise ValueError('That mode is unavailable. Choose: ' + ', '.join(MODE_LABELS[m] for m in policy.modes) + '.')
    if mode != draft.get('mode'):
        # Mode-specific fields and payment selection must not leak across modes.
        draft['fields'] = {k: v for k, v in draft.get('fields', {}).items() if k in ('name', 'phone')}
        draft.pop('address_components', None)
        draft.update(mode=mode, payment_method=None)
        drop_quote(draft)


def _set_payment(draft, value, policy):
    mode = policy.modes.get(draft.get('mode'))
    methods = mode.payment_methods if mode else sorted({m for p in policy.modes.values() for m in p.payment_methods})
    if value not in methods:
        raise ValueError('Choose a payment method: ' + ', '.join(methods) + '.')
    draft['payment_method'] = value


def _set_field(draft, field, value):
    from orders.checkout_config import FIELDS
    if field not in FIELDS.get(draft.get('mode'), ('name', 'phone')):
        raise ValueError('That field does not apply to the selected mode.')
    value = value.strip()
    if not value or len(value) > (1000 if field == 'address' else 100):
        raise ValueError('Please provide a shorter, nonempty value.')
    if field == 'phone' and not re.fullmatch(r'\+?[\d ()-]{7,25}', value):
        raise ValueError('Please enter a valid phone number, including country code.')
    draft.setdefault('fields', {})[field] = value.upper() if field == 'postal_code' else value
    if field == 'address':
        # A labelled full replacement supersedes the structured address task.
        draft.pop('address_components', None)
    elif field == 'postal_code' and draft.get('address_components'):
        set_delivery_address(draft, {**draft['address_components'], 'postal_code': value.upper()})


def _update_action(draft, resolved, policy):
    """Typed values enter existing policy validation without interpreting prose."""
    action = resolved.proposal
    if action.kind == 'SET_FULFILLMENT':
        _set_mode(draft, action.value, policy)
        return 'mode'
    elif action.kind == 'SET_PAYMENT_METHOD':
        _set_payment(draft, action.value, policy)
        return 'payment_method'
    elif action.kind == 'CLEAR_CHECKOUT_FIELD':
        if action.field == 'discount_code':
            draft.pop('discount_code', None)
        else:
            draft.setdefault('fields', {}).pop(action.field, None)
            if action.field == 'address':
                draft.pop('address_components', None)
                draft['fields'].pop('postal_code', None)
            elif action.field == 'postal_code' and draft.get('address_components'):
                components = deepcopy(draft['address_components'])
                components.pop('postal_code', None)
                set_delivery_address(draft, components)
        drop_quote(draft)
        return action.field
    elif action.kind == 'SET_CHECKOUT_FIELD':
        if action.field == 'discount_code':
            draft['discount_code'] = action.value.strip()
            drop_quote(draft)
        else:
            _set_field(draft, action.field, action.value)
        return action.field
    return None


@transaction.atomic
def advance_checkout(*, tenant, customer, chat_id, platform, basket, checklist, text, configuration,
                     reset_address=False, original_text=None, action=None, defer_quote=None, delivery_address=None):
    """Returns (reply, confirmed order or None, needs reply). No provider I/O here."""
    if customer.tenant_id != tenant.pk:
        raise ValueError('Checkout customer does not belong to this tenant.')
    from commerce.menu_sync import lock_menu
    lock_menu(tenant.pk)
    policy = CheckoutPolicy.model_validate(configuration)
    session = ChatSession.objects.select_for_update().filter(
        tenant=tenant, session_id=str(chat_id), platform=platform,
        is_completed=False).order_by('-last_interaction_at', '-created_at', '-pk').first()
    if session is None or session.customer_id != customer.pk:
        raise ValueError('No active checkout session was found. Please start a new chat.')
    if session.order_id:
        order = Order.objects.select_for_update().get(pk=session.order_id, tenant=tenant, customer=customer)
        checklist.update(order=True, order_id=str(order.pk), payment=order.payment_status == Order.PaymentStatus.PAID)
        if (action and action.proposal.kind in {'SET_FULFILLMENT', 'SET_PAYMENT_METHOD', 'SET_CHECKOUT_FIELD', 'CLEAR_CHECKOUT_FIELD'}
                or not action and (mode_request(text) or text.lower() in ('cash', 'online')
                                   or text.lower().startswith(('schedule', 'table', 'address')))):
            from .order_support import store_call_response
            return store_call_response(tenant), None, False
        saved_basket = (session.state or {}).get('checkout', {}).get('basket')
        if saved_basket and saved_basket != basket.to_dict():
            return 'Your basket differs from the confirmed order. Please contact the café before paying.', None, False
        return None, order, False
    draft = deepcopy((session.state or {}).get('checkout', {'fields': {}}))
    if action is None and text.lower() in ('cancel checkout', 'cancel', 'stop'):
        session.state = {**(session.state or {}), 'checkout': {}}
        session.save(update_fields=['state', 'last_interaction_at'])
        checklist.pop('checkout', None)
        return 'Checkout stopped. Your basket is saved.', None, False
    preference = checklist.get('fulfillment_preference')
    if not draft.get('mode') and preference in policy.modes:
        draft['mode'] = preference
    if not draft.get('mode') and len(policy.modes) == 1:
        draft['mode'] = next(iter(policy.modes))
    if reset_address and draft.get('mode') == 'delivery':
        draft.pop('address_components', None)
        for field in ('address', 'postal_code'):
            draft.setdefault('fields', {}).pop(field, None)
        drop_quote(draft)
        draft['awaiting'] = 'address'
    previous_quote = draft.get('quote')
    from chatbot_core.logic.outcomes import TaskOutcome
    draft['outcome'] = TaskOutcome.NEEDS_CLARIFICATION.value
    updated_field = None
    effective_now = _checkout_now(tenant)
    candidate = deepcopy(draft)
    try:
        if action is not None:
            updated_field = _update_action(candidate, action, policy)
        else:
            updated_field = _update(candidate, text.strip(), policy)
        if (candidate.get('mode') == 'delivery' and not reset_address
                and not candidate.get('fields', {}).get('address')
                and 'address_components' not in candidate and delivery_address
                and updated_field != 'address'):
            components = deepcopy(delivery_address)
            if updated_field == 'postal_code':
                components.pop('postal_code', None)
                if candidate['fields'].get('postal_code'):
                    components['postal_code'] = candidate['fields']['postal_code']
            set_delivery_address(candidate, components)
        _validate_checkout_field(policy, candidate, updated_field, effective_now)
    except (ValueError, KeyError) as exc:
        # A rejected replacement must not leave either the invalid value or an
        # older value that the customer just tried to supersede confirmable.
        if updated_field in ('scheduled_at', 'postal_code'):
            draft.setdefault('fields', {}).pop(updated_field, None)
            if updated_field == 'postal_code' and draft.get('address_components'):
                components = deepcopy(draft['address_components'])
                components.pop('postal_code', None)
                set_delivery_address(draft, components)
        drop_quote(draft)
        current, awaiting, question = None, updated_field or draft.get('awaiting'), str(exc)
    else:
        draft = candidate
        if updated_field == 'mode':
            checklist['fulfillment_preference'] = draft['mode']
        try:
            if defer_quote:
                current, awaiting, question = None, 'basket', defer_quote
                draft['outcome'] = TaskOutcome.TEMPORARILY_BLOCKED.value
            else:
                current, awaiting, question = quote(policy, draft, basket, tenant, now=effective_now)
        except (ValueError, KeyError) as exc:
            drop_quote(draft)
            draft['outcome'] = TaskOutcome.TEMPORARILY_BLOCKED.value
            current, awaiting, question = None, draft.get('awaiting'), str(exc)
    draft['basket'] = deepcopy(basket.to_dict())
    draft['awaiting'] = awaiting
    if not current and isinstance(previous_quote, dict):
        remember_invalidated_quote(draft, previous_quote)
    draft['quote'] = current
    if current:
        draft.pop('invalidated_quote', None)
    session.state = {**(session.state or {}), 'checkout': draft}
    session.save(update_fields=['state', 'last_interaction_at'])
    checklist['checkout'] = deepcopy(draft)
    if delivery_address is not None:
        if 'address_components' in draft:
            delivery_address.clear()
            delivery_address.update(deepcopy(draft['address_components']))
        elif reset_address or updated_field == 'address' or (updated_field == 'mode' and draft.get('mode') != 'delivery'):
            delivery_address.clear()
    if not current:
        return question, None, True
    confirmed = (action.proposal.kind == 'CONFIRM_ORDER' and action.quote_fingerprint == current['fingerprint']
                 if action is not None else bool(CONFIRM.fullmatch((original_text if original_text is not None else text).strip())))
    if not confirmed or not previous_quote or previous_quote['fingerprint'] != current['fingerprint']:
        timing = (f"Scheduled: {current['scheduled_at']}. " if current['scheduled_at'] else
                  f"Preparation: {current['preparation_minutes']} minutes. ")
        fields = '; '.join(f'{k.replace("_", " ")}: {v}' for k, v in current['fields'].items())
        return (f"{MODE_LABELS[current['mode']].title()}. {fields}. {timing}"
                f"Basket: {current['subtotal']}; fee: {current['fee']}; total: {current['total']}. "
                + (f"Currency: {current['commerce']['currency']}. Tax: {Decimal(current['commerce']['tax_minor']) / (10 ** current['commerce']['exponent'])}; discount: {Decimal(current['commerce']['discount_minor']) / (10 ** current['commerce']['exponent'])}; packaging: {Decimal(current['commerce']['policy']['packaging_minor']) / (10 ** current['commerce']['exponent'])}. " if current.get('commerce') else f"Tax: {current['tax']}. ") +
                f"Payment: {current['payment_method']}. Reply confirm to place the order, or change your checkout details."), None, True
    try:
        with transaction.atomic():
            order = create_order(tenant, customer, basket, chat_id, payment_mode=current['payment_method'], catalog_tax=not bool(current.get('commerce')))
            order.order_type = {'delivery': 'H', 'pickup': 'P', 'dine_in': 'D'}[current['mode']]
            order.enable_delivery = current['mode'] == 'delivery'
            order.delivery_charges = Decimal(current['fulfillment_fee']) if order.enable_delivery else Decimal('0')
            order.packing_charges = Decimal(current['fulfillment_fee']) if current['mode'] == 'pickup' else Decimal('0')
            order.service_charge = Decimal(current['fulfillment_fee']) if current['mode'] == 'dine_in' else Decimal('0')
            order.packing_charges += Decimal(current['packaging_fee'])
            order.tax_amount = Decimal(current.get('tax', '0'))
            order.total_amount = Decimal(current['total'])
            order.payment_type = 'COD' if current['payment_method'] == 'cash' else 'ONLINE'
            order.meta = {**order.meta, 'checkout': current}
            if current['scheduled_at']:
                scheduled = datetime.fromisoformat(current['scheduled_at'])
                order.advanced_order = 'Y'
                order.preorder_date = scheduled.date()
                order.preorder_time = scheduled.time().replace(tzinfo=None)
            if current.get('commerce'):
                from commerce.pricing import major
                price = current['commerce']
                order.tax_amount = major(price['tax_minor'], price['exponent'])
                order.discount_amount = major(price['discount_minor'], price['exponent'])
            order.save()
            if current.get('commerce'):
                from commerce.services import accept_order
                accept_order(order, current['commerce'])
    except ValueError as exc:
        draft['outcome'] = TaskOutcome.TEMPORARILY_BLOCKED.value
        session.state = {**(session.state or {}), 'checkout': draft}
        session.save(update_fields=['state', 'last_interaction_at'])
        checklist['checkout'] = deepcopy(draft)
        return str(exc), None, True
    session.order = order
    session.save(update_fields=['order', 'last_interaction_at'])
    checklist.update(order=True, order_id=str(order.pk), payment=False)
    return None, order, False


def address_question(missing):
    labels = {'street_address': 'street address', 'postal_code': 'valid 6-digit pincode'}
    return 'Please share the following address details: ' + ', '.join(labels.get(key, key) for key in missing) + '.'


def set_delivery_address(draft, address):
    """Project a structured draft, including partial fields, into checkout."""
    from .location_utils import normalize_address, format_address, street_address, get_missing_address_keys
    components = normalize_address(address)
    if address.get('address_id'):
        # This identity comes from the address task, never from extraction.
        components['address_id'] = address['address_id']
    fields = draft.setdefault('fields', {})
    before = (deepcopy(draft.get('address_components')), fields.get('address'), fields.get('postal_code'))
    draft['address_components'] = components
    for field in ('address', 'postal_code'):
        fields.pop(field, None)
    if street_address(components):
        fields['address'] = format_address(components)
    if components.get('postal_code'):
        fields['postal_code'] = components['postal_code']
    after = (components, fields.get('address'), fields.get('postal_code'))
    if before != after:
        drop_quote(draft)
        missing = get_missing_address_keys(components)
        draft['awaiting'] = ('postal_code' if missing == ['postal_code'] else 'address') if missing else None


@transaction.atomic
def sync_delivery_address(*, tenant_id, customer, chat_id, platform, checklist, address):
    """Invalidate the quote whenever the separate address task changes its draft."""
    session = ChatSession.objects.select_for_update().filter(
        tenant_id=tenant_id, session_id=str(chat_id), platform=platform,
        is_completed=False).order_by('-last_interaction_at', '-created_at', '-pk').first()
    if not session or session.order_id:
        return
    if customer is None or session.customer_id != customer.pk:
        raise ValueError('Saved checkout does not match this customer')
    draft = deepcopy((session.state or {}).get('checkout'))
    if not draft or draft.get('mode') != 'delivery':
        return
    set_delivery_address(draft, address)
    session.state = {**session.state, 'checkout': draft}
    session.save(update_fields=['state', 'last_interaction_at'])
    checklist['checkout'] = deepcopy(draft)
