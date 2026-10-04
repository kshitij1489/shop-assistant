"""Keep command provenance private, outside provider payloads and wire schemas."""
from contextlib import contextmanager, nullcontext

from django.db import transaction

from .context import current, enabled, assert_scope, activate
from .ownership import worker_ticket, resolve_ticket
from .telemetry import emit


def command_created(command):
    ctx = current()
    if ctx is None:
        return
    assert_scope(command.connection.location.tenant_id)
    if command.accepted_order_id:
        assert_scope(command.accepted_order.order.tenant_id, command.accepted_order.order.customer_id)
    from chatbot_core.models import TenantInfo
    from evaluate.fixtures.provision import OWNER_KEY
    with transaction.atomic():
        tenant = TenantInfo.objects.select_for_update().get(pk=ctx.tenant_id)
        owner = tenant.meta[OWNER_KEY]
        if owner['lease'] != ctx.lease_id:
            raise PermissionError('Commerce command lease mismatch')
        owner.setdefault('application_command_contexts', {}).setdefault(str(command.pk), worker_ticket(ctx))
        tenant.save(update_fields=['meta'])
    emit('commerce.command.enqueued', command_id=str(command.pk),
         order_id=str(command.accepted_order.order_id) if command.accepted_order_id else None,
         operation=command.kind, status=command.status)


@contextmanager
def command_effect(command):
    """Restore stored request provenance around business effects for this command.

    Telemetry and side effects that enqueue follow-on commands must observe the
    originating tenant, lease, and request. Provider payloads never enter here.
    """
    if not enabled():
        yield None
        return
    tenant = command.connection.location.tenant
    owner = (tenant.meta or {}).get('evaluation_owned_v1', {})
    value = owner.get('application_command_contexts', {}).get(str(command.pk))
    if not value:
        yield None
        return
    ctx, _ = resolve_ticket(value, worker=True)
    with activate(ctx):
        assert_scope(tenant.pk)
        if command.accepted_order_id:
            assert_scope(command.accepted_order.order.tenant_id, command.accepted_order.order.customer_id)
        yield ctx


def command_event(command, event):
    if not enabled():
        return
    with command_effect(command) as ctx:
        if ctx is None:
            return
        emit(event, command_id=str(command.pk), operation=command.kind,
             status=command.status, retries=max(command.attempts - 1, 0))


@contextmanager
def originating_effect(connection, event_payload):
    """Activate the originating command's context for an adapter event delivery."""
    command = _originating_command(connection, event_payload)
    if command is None:
        yield None
        return
    with command_effect(command) as ctx:
        yield ctx


def _originating_command(connection, event_payload):
    """Locate the command whose stored context should cover this event's effects."""
    if not isinstance(event_payload, dict):
        return None
    data = event_payload.get('data') or {}
    event_type = data.get('type')
    from commerce.models import Command
    if event_type == 'payment.updated':
        payment_id = data.get('payment_id')
        if not payment_id:
            return None
        return (Command.objects.filter(connection=connection, kind='payment.create')
                .filter(payload__payment_id=str(payment_id)).order_by('created_at').first())
    if event_type == 'order.updated':
        accepted_order_id = data.get('accepted_order_id')
        if not accepted_order_id:
            return None
        return (Command.objects.filter(connection=connection, kind='order.submit')
                .filter(payload__accepted_order_id=str(accepted_order_id)).order_by('created_at').first())
    return None


def effect_scope_for_command_id(command_id):
    """Return a context manager that restores provenance for an ack/claim target."""
    if not enabled() or not command_id:
        return nullcontext(None)
    from commerce.models import Command
    try:
        command = Command.objects.select_related(
            'connection__location__tenant', 'accepted_order__order').get(pk=command_id)
    except (Command.DoesNotExist, ValueError, TypeError):
        return nullcontext(None)
    return command_effect(command)
