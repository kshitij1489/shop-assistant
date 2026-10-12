"""Private provenance survives asynchronous commerce work, outside wire payloads."""
from contextlib import contextmanager
from copy import deepcopy
import logging

from django.conf import settings
from django.db import transaction

from .capture import Recording, current, recording, snapshot

logger = logging.getLogger(__name__)


def origin_for(tenant_id):
    recorder = current()
    if recorder and recorder.identity['tenant_id'] == str(tenant_id):
        return dict(recorder.identity)
    return {}


def committed(record, event, **payload):
    """Snapshot now, publish only after the commerce transaction commits."""
    if not getattr(settings, 'CONVERSATION_RECORDING_ENABLED', False) or not record.recording_context:
        return
    try:
        tenant = record.location.tenant
        recorder = Recording(tenant, '', '', origin=deepcopy(record.recording_context), kind='commerce')
        captured = snapshot({'accepted_order': record, **payload}, recorder.secrets)
        transaction.on_commit(lambda: recorder.related(event, kind='commerce', **captured), robust=True)
    except Exception as exc:
        active = current()
        if active:
            with active.lock:
                active.dropped += 1
        logger.warning('Commerce recording capture failed error_type=%s', type(exc).__name__)


def command_committed(command, event):
    if getattr(settings, 'CONVERSATION_RECORDING_ENABLED', False) and command.accepted_order_id:
        committed(command.accepted_order, event, command=command)


@contextmanager
def event_scope(connection, payload):
    record = None
    if getattr(settings, 'CONVERSATION_RECORDING_ENABLED', False):
        from commerce.models import AcceptedOrder, Payment
        data = payload.get('data') or {}
        try:
            if data.get('type') == 'payment.updated':
                payment = Payment.objects.filter(connection=connection, pk=data.get('payment_id')).select_related('accepted_order').first()
                record = payment.accepted_order if payment else None
            elif data.get('type') == 'order.updated':
                record = AcceptedOrder.objects.filter(location=connection.location,
                    pk=data.get('accepted_order_id'), commands__connection=connection,
                    commands__kind='order.submit').first()
        except (ValueError, TypeError):
            pass
    if record is None or not record.recording_context:
        yield
        return
    tenant = connection.location.tenant
    with recording(tenant, '', '', origin=record.recording_context, kind='commerce'):
        yield
