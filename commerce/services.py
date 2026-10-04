from collections import defaultdict
from datetime import timedelta
from copy import deepcopy
from django.apps import apps
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from orders.models import Order, MenuItem
from .models import (Configuration, Connection, AcceptedOrder, StockItem, Reservation,
                     Payment, Command, ReconciliationIssue)
from .policy import Policy
from .pricing import calculate, digest, major


def configuration(tenant):
    if not apps.is_installed('commerce'):
        return None
    return Configuration.objects.filter(tenant=tenant, enabled=True).select_related('location').first()


def basket_quote(tenant, basket, *, mode, fee='0', discount_code=''):
    config = configuration(tenant)
    if not config:
        return None
    from chatbot_core.logic.cafe.catalog import load_catalog, validate_selection
    catalog = load_catalog(tenant.api_key)
    selections = [validate_selection(catalog, e['item_id'], e['item_variant_id'], e['quantity'], e.get('modifiers', [])) for e in basket.items]
    result = calculate(selections, config.policy, mode=mode, fee=fee, discount_code=discount_code)
    result['location_id'] = str(config.location_id)
    return result


def issue(record, code, **detail):
    return ReconciliationIssue.objects.get_or_create(accepted_order=record, code=code, resolved_at__isnull=True, defaults={'detail': detail})[0]


def enqueue(connection, kind, key, record, payload):
    from evaluate.controls.context import current, assert_scope
    if current():
        assert_scope(connection.location.tenant_id)
        if record is not None:
            assert_scope(record.order.tenant_id, record.order.customer_id)
    if record is not None and record.location_id != connection.location_id:
        raise ValueError('Order does not belong to this adapter location.')
    if kind not in connection.capabilities:
        raise ValueError(f'Adapter does not support {kind}.')
    command = Command.objects.get_or_create(connection=connection, dedupe_key=key,
        defaults=dict(kind=kind, accepted_order=record, payload=payload, available_at=timezone.now()))[0]
    from evaluate.controls.commerce import command_created
    command_created(command)
    return command


def refund_capture(record, payment):
    if 'payment.refund' not in payment.connection.capabilities or payment.refunded_minor >= payment.captured_minor:
        return
    # A cumulative target stays stable across refund-progress observations.
    # Adapters reconcile this target against provider refunds before moving money.
    return enqueue(payment.connection, 'payment.refund', f'refund:{payment.pk}:{payment.captured_minor}', record,
                   {'payment_id': str(payment.pk), 'external_id': payment.external_id,
                    'currency': payment.currency, 'exponent': record.exponent,
                    'target_refunded_minor': payment.captured_minor})


def pos_submit(record):
    connection = Connection.objects.filter(location=record.location, role='pos', active=True).first()
    if not connection or 'order.submit' not in connection.capabilities:
        record.pos_state = 'unconfigured'
        issue(record, 'pos_unconfigured')
    else:
        enqueue(connection, 'order.submit', f'order:{record.pk}', record,
                {'accepted_order_id': str(record.pk), 'order_id': str(record.order_id),
                 'snapshot_hash': record.snapshot_hash, 'snapshot': record.snapshot,
                 'payment_method': record.order.payment_mode,
                 'payments': [{'payment_id': str(p.pk), 'external_id': p.external_id,
                               'provider': p.connection.provider, 'currency': p.currency,
                               'captured_minor': p.captured_minor, 'refunded_minor': p.refunded_minor}
                              for p in record.payments.select_related('connection').all()],
                 'reservations': [{'id': str(r.pk), 'stock_id': str(r.stock_id), 'quantity': r.quantity} for r in record.reservations.all()]})
        record.pos_state = 'pending'
        record.issues.filter(code='pos_unconfigured', resolved_at__isnull=True).update(
            resolved_at=timezone.now(), resolved_by='system:pos_submit',
            resolution_evidence='An order.submit command is durably queued for the configured POS connection.',
            resolution_note='POS configuration recovered. Provider acceptance still requires an order.updated event.')
    record.save(update_fields=['pos_state'])


def reserve(record, policy):
    if policy.stock_policy == 'untracked':
        return
    demands = defaultdict(int)
    for line in record.snapshot['pricing']['lines']:
        stocks = StockItem.objects.filter(location=record.location)
        stock = stocks.filter(variant_id=line['item_variant_id']).first()
        if stock is None:
            stock = stocks.filter(item_id=line['item_id']).first()
        if stock is None:
            raise ValueError(f"Stock is unknown for {line['name']}.")
        demands[stock.pk] += line['quantity']
        for modifier in line['modifiers']:
            stock = StockItem.objects.filter(location=record.location, addon_id=modifier['option_id']).first()
            if stock:
                demands[stock.pk] += line['quantity'] * modifier['quantity']
    now = timezone.now()
    for stock_id in sorted(demands, key=str):
        stock = StockItem.objects.select_for_update().get(pk=stock_id)
        stock.clean()
        count = demands[stock_id]
        if not stock.available or (stock.authority_id and (not stock.observed_at or stock.observed_at < now - timedelta(seconds=policy.stock_max_age_seconds))):
            raise ValueError('Stock is unavailable or stale. Please try again later.')
        if stock.mode == 'availability':
            if policy.stock_policy == 'strict':
                raise ValueError('Exact stock is required but only availability is known.')
        elif not StockItem.objects.filter(pk=stock_id, on_hand__gte=F('reserved') + F('pending_consumed') + count).update(reserved=F('reserved') + count):
            raise ValueError('An item has just sold out. Please update your basket.')
        if stock.mode == 'availability':
            StockItem.objects.filter(pk=stock_id).update(reserved=F('reserved') + count)
        Reservation.objects.create(accepted_order=record, stock=stock, quantity=count, expires_at=now + timedelta(seconds=policy.reservation_seconds))


class StockCommitmentError(ValueError):
    pass


@transaction.atomic
def finish_reservations(record, consume):
    """Caller locks accepted order first, then stock in a stable order."""
    for hold in record.reservations.filter(state='held').order_by('stock_id'):
        stock = StockItem.objects.select_for_update().get(pk=hold.stock_id)
        if consume and stock.mode == 'quantity' and (
                stock.on_hand < hold.quantity or stock.on_hand < stock.reserved + stock.pending_consumed):
            raise StockCommitmentError('Stock no longer covers committed orders.')
        stock.reserved -= hold.quantity
        if consume and stock.mode == 'quantity':
            if stock.authority_id:
                stock.pending_consumed += hold.quantity
            else:
                stock.on_hand -= hold.quantity
        stock.save(update_fields=['reserved', 'on_hand', 'pending_consumed'])
        hold.state = 'consumed' if consume else 'released'
        hold.save(update_fields=['state'])


@transaction.atomic
def accept_order(order, pricing):
    """Persist price/options/contact facts, inventory and commands atomically."""
    order = Order.objects.select_for_update().get(pk=order.pk)
    if order.customer_id and order.customer.tenant_id != order.tenant_id:
        raise ValueError('Order customer belongs to another tenant.')
    if major(pricing['total_minor'], pricing['exponent']) != order.total_amount:
        raise ValueError('Accepted total does not match the saved order.')
    previous = AcceptedOrder.objects.filter(order=order).first()
    if previous:
        return previous
    config = Configuration.objects.select_for_update().get(tenant=order.tenant, enabled=True)
    if pricing['policy'] != Policy.model_validate(config.policy).model_dump(mode='json') or pricing['location_id'] != str(config.location_id):
        raise ValueError('Commerce settings changed. Review checkout again.')
    checkout = deepcopy(order.meta.get('checkout', {}))
    snapshot = dict(schema_version=1, order_id=str(order.pk), tenant_id=str(order.tenant_id),
                    location_id=str(config.location_id), source=order.source,
                    customer={'id': str(order.customer_id) if order.customer_id else None,
                              'name': order.customer.name if order.customer else '',
                              'phone': order.customer.phone if order.customer else ''},
                    fulfillment=checkout, instructions=order.meta.get('instruction', ''), pricing=deepcopy(pricing))
    for index, line in enumerate(snapshot['pricing']['lines'], start=1):
        line['line_id'] = f'{order.pk}:{index}'
    snapshot['fulfillment']['fulfillment_id'] = f'{order.pk}:fulfillment'
    snapshot['customer'].update({k: v for k, v in checkout.get('fields', {}).items() if k in ('name', 'phone')})
    policy = Policy.model_validate(config.policy)
    record = AcceptedOrder.objects.create(expires_at=timezone.now() + timedelta(seconds=policy.reservation_seconds), order=order, location=config.location, currency=pricing['currency'],
                exponent=pricing['exponent'], total_minor=pricing['total_minor'], snapshot=snapshot, snapshot_hash=digest(snapshot))
    policy = Policy.model_validate(config.policy)
    reserve(record, policy)
    if order.payment_mode == 'online':
        if record.total_minor <= 0:
            raise ValueError('Online checkout requires a positive total. Choose cash for a zero-total order.')
        connection = Connection.objects.filter(location=config.location, role='payment', active=True).first()
        from .credentials import adapter_secret
        if not connection or not {'payment.create', 'payment.reconcile'} <= set(connection.capabilities) or not adapter_secret(connection):
            raise ValueError('An active payment adapter with credentials and creation/reconciliation capabilities is required.')
        payment = Payment.objects.create(accepted_order=record, connection=connection, requested_minor=record.total_minor, currency=record.currency)
        enqueue(connection, 'payment.create', f'payment:{payment.pk}', record,
                {'payment_id': str(payment.pk), 'order_id': str(order.pk), 'currency': record.currency,
                 'exponent': record.exponent, 'amount_minor': record.total_minor,
                 'customer': snapshot['customer'], 'expires_at': record.expires_at.isoformat()})
    else:
        finish_reservations(record, consume=True)
        record.state = 'confirmed'
        record.save(update_fields=['state'])
        pos_submit(record)
    return record


@transaction.atomic
def cancel_order(record_id):
    record = AcceptedOrder.objects.select_for_update().select_related('order').get(pk=record_id)
    if record.state == 'cancelled':
        return record
    if record.state != 'awaiting_payment':
        raise ValueError('Confirmed orders need POS cancellation and explicit refund reconciliation.')
    finish_reservations(record, consume=False)
    record.state = 'cancelled'
    record.save(update_fields=['state'])
    Order.objects.filter(pk=record.order_id).update(order_status='cancelled')
    return record


def expire_reservations():
    ids = AcceptedOrder.objects.filter(state='awaiting_payment', expires_at__lte=timezone.now()).values_list('pk', flat=True)
    count = 0
    for pk in list(ids):
        with transaction.atomic():
            record = AcceptedOrder.objects.select_for_update().get(pk=pk)
            if record.state == 'awaiting_payment' and record.expires_at <= timezone.now():
                finish_reservations(record, consume=False)
                record.state = 'expired'
                record.save(update_fields=['state'])
                Order.objects.filter(pk=record.order_id).update(order_status='cancelled')
                for payment in record.payments.all():
                    if 'payment.reconcile' in payment.connection.capabilities:
                        enqueue(payment.connection, 'payment.reconcile', f'expired:{payment.pk}', record, {'payment_id': str(payment.pk), 'external_id': payment.external_id})
                count += 1
    return count
