"""Context-local business controls; security and transport clocks stay real."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event
from typing import Literal

from django.conf import settings
from django.utils import timezone
from evaluate.contracts.models import ExecutionIdentity


@dataclass(frozen=True)
class ControlContext:
    identity: ExecutionIdentity
    lease_id: str
    tenant_id: str
    customer_id: str
    session_id: str  # ChatSession UUID, never the browser cookie
    request_id: str
    business_at: datetime
    faults: frozenset[str] = frozenset()
    cache_mode: Literal['cold', 'warm'] = 'cold'
    evidence_failed: Event = field(default_factory=Event, compare=False, repr=False)


_current = ContextVar('evaluation_context', default=None)


def enabled():
    return getattr(settings, 'EVALUATION_ENABLED', False) is True


def current():
    return _current.get() if enabled() else None


@contextmanager
def activate(context):
    """Private Python boundary; callers validate ownership before entry."""
    token = _current.set(context if enabled() else None)
    try:
        yield current()
    finally:
        _current.reset(token)


def assert_scope(tenant_id, customer_id=None):
    ctx = current()
    if ctx and (str(tenant_id) != ctx.tenant_id or
                (customer_id is not None and str(customer_id) != ctx.customer_id)):
        raise PermissionError('Evaluation scope does not own this operation')


def business_now(tenant_id=None):
    if tenant_id is not None:
        assert_scope(tenant_id)
    ctx = current()
    return ctx.business_at if ctx else timezone.now()


def fault_active(name, tenant_id):
    assert_scope(tenant_id)
    ctx = current()
    if ctx and name in ctx.faults:
        from .telemetry import emit
        emit('fault.injected', operation=name, injected=True,
             error_type='TimeoutError' if name == 'classification' else 'ServiceUnavailable')
        return True
    return False
