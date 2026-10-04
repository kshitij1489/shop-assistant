"""Reviewed, durably deduplicated application actions."""
from evaluate.contracts.interfaces import Blocked
from evaluate.scenarios.controls import DatasetControls
from .ownership import owned


class ApplicationControls(DatasetControls):
    def capabilities(self):
        return frozenset({'freeze_clock', 'lookup_control'})

    def apply(self, lease, identity, action):
        owned(lease, identity, self.provisioner)
        op = action.operation
        if op.kind == 'lookup_control' and (op.service, op.outcome) not in {
                ('classification', 'timeout'), ('classification', 'success'),
                ('coverage', 'unavailable'), ('coverage', 'success')}:
            raise Blocked('This application lane does not implement that lookup control')
        return super().apply(lease, identity, action)

    def prerequisites(self, tenant, owner, op):
        pass

    def mutate(self, lease, tenant, owner, op):
        controls = owner.setdefault('application_controls', {})
        if op.kind == 'freeze_clock':
            controls['clock'] = op.clock.model_dump()
        elif op.kind == 'lookup_control':
            faults = set(controls.get('faults', []))
            if op.outcome == 'success':
                faults.discard(op.service)
            else:
                faults.add(op.service)
            controls['faults'] = sorted(faults)
        else:
            raise Blocked('Unsupported application action')

    @staticmethod
    def evidence(tenant, owner):
        return dict(application_controls=owner.get('application_controls', {}),
                    successful_lookups='configured_services')


def configure_cache(provisioner, lease, identity, mode):
    """Private integration setup; cache mode must also be in the run manifest."""
    from django.db import transaction
    if mode not in ('cold', 'warm'):
        raise Blocked('Unknown evaluation cache mode')
    owned(lease, identity, provisioner)
    with transaction.atomic():
        tenant, owner = provisioner.owned(lease, lock=True)
        owner.setdefault('application_controls', {})['cache_mode'] = mode
        provisioner.save_owner(tenant, owner)
