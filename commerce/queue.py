"""At-least-once pull delivery with expiring leases and stable command IDs."""
from datetime import timedelta
import uuid
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from .models import Command, Inbox, Payment, AcceptedOrder
from .services import issue, enqueue, expire_reservations


@transaction.atomic
def claim(connection):
    # Serializes claims on this adapter even on databases without SKIP LOCKED.
    type(connection).objects.select_for_update().get(pk=connection.pk)
    now = timezone.now()
    commands = list(Command.objects.select_for_update(of=('self',)).filter(connection=connection, available_at__lte=now)
        .filter(Q(accepted_order__isnull=True) | Q(accepted_order__location_id=connection.location_id))
        .filter(Q(status='pending') | Q(status='leased', lease_until__lte=now)).order_by('created_at', 'id')[:20])
    result = []
    for command in commands:
        if command.kind == 'payment.create' and (command.accepted_order.state != 'awaiting_payment' or command.accepted_order.expires_at <= now):
            command.status = 'failed'
            command.error = 'checkout_expired_or_closed'
            command.save(update_fields=['status', 'error'])
            continue
        if command.attempts >= 10:
            command.status = 'unknown'
            command.save(update_fields=['status'])
            if command.accepted_order_id:
                issue(command.accepted_order, 'command_exhausted', command_id=str(command.pk))
            continue
        command.status, command.lease_token = 'leased', uuid.uuid4()
        command.lease_until = now + timedelta(seconds=120)
        command.attempts += 1
        command.save(update_fields=['status', 'lease_token', 'lease_until', 'attempts'])
        from evaluate.controls.commerce import command_event
        command_event(command, 'commerce.command.claimed')
        result.append({'schema_version': 1, 'command_id': str(command.pk), 'idempotency_key': str(command.pk),
                       'type': command.kind, 'lease_token': str(command.lease_token),
                       'lease_until': command.lease_until.isoformat(), 'attempt': command.attempts, 'data': command.payload})
    return result


@transaction.atomic
def acknowledge(connection, command_id, data):
    command = Command.objects.select_for_update().get(pk=command_id, connection=connection)
    if command.lease_token != data.lease_token:
        raise ValueError('Stale lease token.')
    if command.status == 'succeeded' and data.outcome == 'succeeded':
        return
    if command.status != 'leased' or command.lease_until <= timezone.now():
        raise ValueError('Lease expired or command already acknowledged.')
    command.error = data.error_code
    if data.outcome == 'retry' and command.attempts < 10:
        command.status = 'pending'
        command.available_at = timezone.now() + timedelta(seconds=min(3600, 2 ** command.attempts * 5))
    else:
        command.status = 'unknown' if data.outcome == 'retry' else data.outcome
    command.save(update_fields=['status', 'error', 'available_at'])
    from evaluate.controls.commerce import command_event
    command_event(command, 'commerce.command.acknowledged')
    if command.status in ('unknown', 'failed') and command.accepted_order_id:
        issue(command.accepted_order, 'adapter_' + command.status, command_id=str(command.pk), error_code=data.error_code)


def reconcile():
    from .events import process
    expired = expire_reservations()
    for pk in Inbox.objects.exclude(status='processed').filter(attempts__lt=10).values_list('pk', flat=True)[:100]:
        process(pk)
    # Bucketing gives each unresolved payment/order a fresh read request without
    # reissuing the money-moving command. Query providers using original IDs.
    bucket = int(timezone.now().timestamp()) // 300
    cutoff = timezone.now() - timedelta(minutes=5)
    count = 0
    for pk in AcceptedOrder.objects.filter(state='confirmed', pos_state='unconfigured').values_list('pk', flat=True)[:100]:
        with transaction.atomic():
            from .services import pos_submit
            record = AcceptedOrder.objects.select_for_update().get(pk=pk)
            if record.state == 'confirmed' and record.pos_state == 'unconfigured':
                pos_submit(record)
    for payment in Payment.objects.select_related('connection', 'accepted_order').filter(connection__active=True, updated_at__lt=cutoff).exclude(status__in=['captured', 'refunded', 'failed', 'cancelled'])[:100]:
        if 'payment.reconcile' in payment.connection.capabilities:
            enqueue(payment.connection, 'payment.reconcile', f'reconcile:payment:{payment.pk}:{bucket}', payment.accepted_order,
                    {'payment_id': str(payment.pk), 'external_id': payment.external_id})
            count += 1
    for record in AcceptedOrder.objects.filter(pos_state='pending', created_at__lt=cutoff)[:100]:
        command = record.commands.filter(kind='order.submit').select_related('connection').first()
        if command and command.connection.active and 'order.reconcile' in command.connection.capabilities:
            enqueue(command.connection, 'order.reconcile', f'reconcile:order:{record.pk}:{bucket}', record,
                    {'accepted_order_id': str(record.pk), 'order_id': str(record.order_id), 'original_command_id': str(command.pk)})
            count += 1
    return {'expired': expired, 'reconciliation_commands': count}
