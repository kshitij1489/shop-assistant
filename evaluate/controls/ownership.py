"""Private ownership checks shared by HTTP, workers and state inspection."""
from datetime import datetime
from django.core import signing
from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.contracts.models import Clock, ExecutionIdentity
from .context import ControlContext, enabled

SALT = 'evaluate.controls.v1'
HEADER = 'X-Evaluation-Context'


def owned(lease, identity, provisioner=None):
    if not enabled():
        raise Blocked('Application evaluation controls are disabled')
    if provisioner is None:
        from evaluate.fixtures.provision import DjangoProvisioner
        provisioner = DjangoProvisioner()
    tenant, owner = provisioner.owned(lease)
    if (owner['run_id'], owner['scenario_id'], owner['instance_id'], owner['attempt']) != (
            identity.run_id, identity.scenario_id, identity.scenario_instance_id, identity.attempt):
        raise Blocked('Execution identity does not own this evaluation lease')
    if owner.get('lifecycle') not in ('ready', 'failed_preserved'):
        raise Blocked('Evaluation lease is not available')
    from orders.models import Customer, ChatSession
    customer = owner['maps']['customer:active']
    if not Customer.objects.filter(pk=customer, tenant=tenant).exists():
        raise Blocked('Owned evaluation customer is missing')
    if not ChatSession.objects.filter(tenant=tenant, customer_id=customer, platform='website',
                                     session_id=owner['browser_sessions'][0]).exists():
        raise Blocked('Owned evaluation session is missing')
    return tenant, owner


def context_for(lease, identity, request_id, provisioner=None, controls=None):
    tenant, owner = owned(lease, identity, provisioner)
    policy = controls if controls is not None else owner.get('application_controls', {})
    if set(policy) - {'clock', 'faults', 'cache_mode'}:
        raise Blocked('Unsupported application control')
    clock = Clock.model_validate(policy.get('clock', owner['clock']))
    faults = frozenset(policy.get('faults', []))
    if faults - {'classification', 'coverage'} or policy.get('cache_mode', 'cold') not in ('cold', 'warm'):
        raise Blocked('Unsupported application control')
    # IDs are validated using the same contract as the runner.
    from evaluate.contracts.models import ID
    from pydantic import TypeAdapter
    request_id = TypeAdapter(ID).validate_python(request_id)
    ctx = ControlContext(identity, lease.handle, str(tenant.pk), str(owner['maps']['customer:active']),
        str(owner['maps']['session:active']), request_id, datetime.fromisoformat(clock.at),
        faults, policy.get('cache_mode', 'cold'))
    return ctx, owner


def ticket(lease, identity, request_id, provisioner=None):
    context_for(lease, identity, request_id, provisioner)
    return signing.dumps(dict(lease=lease.handle, identity=identity.model_dump(), request_id=request_id), salt=SALT)


def worker_ticket(ctx):
    # Snapshot the business clock and controls at dispatch, not worker start.
    from zoneinfo import ZoneInfo
    clock = dict(at=ctx.business_at.astimezone(ZoneInfo('UTC')).isoformat(), timezone='UTC')
    return signing.dumps(dict(lease=ctx.lease_id, identity=ctx.identity.model_dump(), request_id=ctx.request_id,
        controls=dict(clock=clock, faults=sorted(ctx.faults), cache_mode=ctx.cache_mode)), salt=SALT + '.worker')


def assert_session(tenant_id, user_id, platform):
    from .context import current, assert_scope
    ctx = current()
    if ctx is None:
        return
    assert_scope(tenant_id)
    _, owner = owned(Lease(ctx.lease_id, ctx.identity.scenario_instance_id), ctx.identity)
    if platform != 'website' or str(user_id) != owner['browser_sessions'][0]:
        raise Blocked('Queued message does not belong to the owned evaluation browser')


def resolve_ticket(value, *, worker=False):
    data = signing.loads(value, salt=SALT + ('.worker' if worker else ''),
                         max_age=86400 if worker else 300)
    identity = ExecutionIdentity.model_validate(data['identity'])
    return context_for(Lease(data['lease'], identity.scenario_instance_id), identity,
                       data['request_id'], controls=data.get('controls') if worker else None)
