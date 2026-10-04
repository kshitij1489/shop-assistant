from datetime import timedelta
from urllib.parse import urlsplit
from django.db import transaction, IntegrityError
from django.utils import timezone
from orders.models import Order
from .models import AcceptedOrder, Payment, StockItem, Reservation, Inbox, ExternalMapping
from .schemas import Event, PaymentUpdate, StockUpdate
from .pricing import digest
from .services import issue, finish_reservations, pos_submit, refund_capture, StockCommitmentError


def receive(connection, payload):
    event = Event.model_validate(payload)
    capability = {'payment.updated': 'payment.create', 'inventory.updated': 'inventory.update', 'order.updated': 'order.submit'}[event.data.type]
    role = 'payment' if event.data.type == 'payment.updated' else 'pos'
    if connection.role != role or capability not in connection.capabilities:
        raise ValueError('Event is outside this connection’s capabilities.')
    canonical = event.model_dump(mode='json')
    row, _ = Inbox.objects.get_or_create(connection=connection, event_id=event.event_id,
        defaults={'event_type': event.data.type, 'payload': canonical, 'payload_hash': digest(canonical)})
    if row.payload_hash != digest(canonical):
        raise ValueError('Event ID was reused with a different payload.')
    process(row.pk)
    row.refresh_from_db()
    return row


def process(inbox_id):
    # Inbox receipt is committed before processing. A crash or dependency failure
    # leaves a durable retryable event; its effects commit in one transaction.
    with transaction.atomic():
        inbox = Inbox.objects.select_for_update().select_related('connection').get(pk=inbox_id)
        if inbox.status == 'processed':
            return
        inbox.attempts += 1
        try:
            with transaction.atomic():
                from contextlib import nullcontext
                from django.conf import settings
                scope = nullcontext()
                if getattr(settings, 'EVALUATION_ENABLED', False):
                    from evaluate.controls.commerce import originating_effect
                    scope = originating_effect(inbox.connection, inbox.payload)
                with scope:
                    event = Event.model_validate(inbox.payload)
                    if isinstance(event.data, PaymentUpdate):
                        payment_update(inbox.connection, event.data)
                    elif isinstance(event.data, StockUpdate):
                        stock_update(inbox.connection, event.data)
                    else:
                        order_update(inbox.connection, event.data)
        except (ValueError, IntegrityError, AcceptedOrder.DoesNotExist, Payment.DoesNotExist, StockItem.DoesNotExist, Reservation.DoesNotExist) as exc:
            inbox.status = 'failed'
            # No provider response bodies or secrets in operational error fields.
            inbox.error = type(exc).__name__
        else:
            inbox.status, inbox.error, inbox.processed_at = 'processed', '', timezone.now()
        inbox.save(update_fields=['status', 'attempts', 'error', 'processed_at'])


def payment_update(connection, data):
    payment = Payment.objects.get(pk=data.payment_id, connection=connection)
    record = AcceptedOrder.objects.select_for_update().get(pk=payment.accepted_order_id, location=connection.location)
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if data.sequence <= payment.sequence:
        return
    if data.currency != payment.currency or (payment.external_id and data.external_id != payment.external_id):
        issue(record, 'payment_identity_mismatch', payment_id=str(payment.pk))
        return
    if data.refunded_minor > data.captured_minor or data.captured_minor < payment.captured_minor or data.refunded_minor < payment.refunded_minor:
        raise ValueError('Payment cumulative amounts cannot decrease or refund more than captured.')
    if data.status in ('pending', 'authorized', 'failed', 'cancelled') and data.captured_minor:
        raise ValueError('Captured money requires captured/refunded status.')
    if data.checkout_url:
        parsed = urlsplit(data.checkout_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError('A payment checkout URL must be HTTPS without user information.')
        payment.checkout_url = data.checkout_url
    payment.external_id, payment.sequence = data.external_id, data.sequence
    payment.status, payment.captured_minor, payment.refunded_minor = data.status, data.captured_minor, data.refunded_minor
    payment.save()
    if data.captured_minor:
        review_code = None
        detail = {'payment_id': str(payment.pk)}
        if data.captured_minor != payment.requested_minor:
            review_code = 'payment_amount_mismatch'
            detail['captured_minor'] = data.captured_minor
        elif data.refunded_minor:
            review_code = 'refund_received'
            detail['refunded_minor'] = data.refunded_minor
        elif record.state == 'awaiting_payment':
            if record.expires_at <= timezone.now():
                review_code = 'late_payment'
            else:
                try:
                    finish_reservations(record, consume=True)
                except StockCommitmentError:
                    review_code = 'stock_commitment_shortfall'
                else:
                    Order.objects.filter(pk=record.order_id).update(payment_status='paid')
                    record.state = 'confirmed'
                    pos_submit(record)
        elif record.state in ('expired', 'cancelled') or (record.state == 'review' and record.pos_state == 'not_requested'):
            review_code = 'late_payment'
        if review_code:
            record.state = 'review'
            Order.objects.filter(pk=record.order_id).update(payment_status='unpaid')
            issue(record, review_code, **detail)
            finish_reservations(record, consume=False)
            # Refund observations on fulfilled orders remain an explicit review;
            # an unfulfilled capture must return all money still held.
            if review_code != 'refund_received' or record.pos_state == 'not_requested':
                refund_capture(record, payment)
    elif data.status in ('failed', 'cancelled') and record.state == 'awaiting_payment':
        finish_reservations(record, consume=False)
        record.state = 'cancelled'
        Order.objects.filter(pk=record.order_id).update(payment_status='failed', order_status='cancelled')
    record.save(update_fields=['state'])


def stock_update(connection, data):
    stock = StockItem.objects.select_for_update().get(pk=data.stock_id, location=connection.location, authority=connection)
    if data.sequence <= stock.sequence:
        return
    if data.observed_at > timezone.now() + timedelta(seconds=60) or (stock.observed_at and data.observed_at < stock.observed_at):
        raise ValueError('Stock observation time is invalid or out of order.')
    for reservation_id in set(data.acknowledged_reservation_ids):
        hold = Reservation.objects.get(pk=reservation_id, stock=stock, state='consumed')
        if not hold.acknowledged:
            if stock.mode == 'quantity':
                stock.pending_consumed -= hold.quantity
            hold.acknowledged = True
            hold.save(update_fields=['acknowledged'])
    stock.sequence, stock.observed_at = data.sequence, data.observed_at
    if stock.mode == 'quantity' and data.on_hand < stock.reserved + stock.pending_consumed:
        raise ValueError('Stock update would violate local commitments.')
    stock.on_hand, stock.available = data.on_hand, data.available
    stock.save()


def order_update(connection, data):
    record = AcceptedOrder.objects.select_for_update().get(pk=data.accepted_order_id, location=connection.location)
    if not record.commands.filter(connection=connection, kind='order.submit').exists():
        raise ValueError('Order was not submitted to this connection.')
    mapping, _ = ExternalMapping.objects.get_or_create(connection=connection, kind='order', scope='', canonical_id=str(record.order_id),
        defaults={'external_id': data.external_id, 'metadata': {'sequence': 0}})
    if mapping.external_id != data.external_id:
        raise ValueError('Order external identity changed.')
    if data.sequence <= mapping.metadata.get('sequence', 0):
        return
    mapping.metadata = {**mapping.metadata, 'sequence': data.sequence}
    mapping.save(update_fields=['metadata'])
    if data.status in ('rejected', 'cancelled'):
        record.state, record.pos_state = 'review', data.status
        issue(record, 'pos_' + data.status)
        # Consumed inventory is not automatically restocked: the kitchen may
        # already have prepared it. Refunds/restocking are separate decisions.
    else:
        ranks = {'accepted': 1, 'preparing': 2, 'dispatched': 3, 'delivered': 4}
        if record.pos_state in ('rejected', 'cancelled') or ranks.get(data.status, 0) < ranks.get(record.pos_state, 0):
            issue(record, 'pos_status_conflict')
            return
        record.pos_state = data.status
        Order.objects.filter(pk=record.order_id).update(order_status=data.status)
    record.save(update_fields=['state', 'pos_state'])
