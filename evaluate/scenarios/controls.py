"""Shared ScenarioControls implementation with durable deduplication and evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from uuid import uuid4
from django.db import transaction
from django.contrib.sessions.backends.db import SessionStore
from orders import models as om
from commerce import models as cm
from evaluate.contracts.interfaces import Blocked
from evaluate.contracts.models import ExecutionEvent
from evaluate.identity import canonical_hash
from evaluate.fixtures.definitions import get_fixture
from evaluate.fixtures.seeds import apply_fixture


class DatasetControls:
    def __init__(self, provisioner, plan, evidence_writer=None):
        self.provisioner, self.plan, self.writer = provisioner, plan, evidence_writer

    def capabilities(self):
        return self.provisioner.runtime.capabilities()

    def before_turn(self, lease, identity, scenario, original_turn_index):
        review = self.plan.scenarios.get(scenario.scenario_id)
        if (review is None or scenario.scenario_id != identity.scenario_id or
                scenario.source_hash != review.source_hash or scenario.actions != review.actions):
            raise Blocked('Scenario actions differ from the reviewed execution plan')
        _, owner = self.provisioner.owned(lease)
        self.validate_identity(owner, identity)
        if original_turn_index is not None and original_turn_index not in {t.original_turn_index for t in scenario.turns}:
            raise Blocked('Boundary is not an original user turn index')
        return [self.apply(lease, identity, action) for action in scenario.actions
                if action.original_turn_index == original_turn_index]

    def apply(self, lease, identity, action):
        if action.operation.kind not in self.capabilities():
            raise Blocked('Unsupported typed action')
        review = self.plan.scenarios.get(identity.scenario_id)
        reviewed = next((a for a in review.actions if a.action_id == action.action_id), None) if review else None
        if reviewed != action:
            raise Blocked('Action is not the exact reviewed plan action')
        digest = canonical_hash(action.model_dump())
        with transaction.atomic():
            tenant, owner = self.provisioner.owned(lease, lock=True)
            self.validate_identity(owner, identity)
            previous = owner['action_ledger'].get(action.action_id)
            if previous:
                if previous['action_hash'] != digest:
                    raise Blocked('Action ID was reused with changed content')
                if previous['event']['status'] != 'succeeded':
                    raise Blocked('Previous action is incomplete or failed; inspect before retrying')
                event = ExecutionEvent.model_validate(previous['event'])
                self.emit(event)
                return event
            # All preceding listed actions must complete before this one.
            earlier = review.actions[:review.actions.index(action)]
            if any(a.action_id not in owner['action_ledger'] or owner['action_ledger'][a.action_id]['event']['status'] != 'succeeded' for a in earlier):
                raise Blocked('Action prerequisites have not completed in plan order')
            self.prerequisites(tenant, owner, action.operation)
            before = self.evidence(tenant, owner)
            event = ExecutionEvent(**identity.model_dump(), event_id=str(uuid4()), occurred_at=datetime.now(timezone.utc).isoformat(),
                kind='action', original_turn_index=action.original_turn_index, action_id=action.action_id,
                status='started', detail='Typed action started; before evidence in owned ledger')
            owner['action_ledger'][action.action_id] = dict(action_hash=digest, before=before, event=event.model_dump())
            self.provisioner.save_owner(tenant, owner)
        try:
            external_payment = action.operation.kind == 'payment_control'
            external_fixture = (action.operation.kind == 'seed_fixture'
                and get_fixture(action.operation.fixture_id, action.operation.fixture_hash).kind == 'terminal_order')
            if external_payment or external_fixture:
                # The HTTPS callback can enqueue POS work and record provenance
                # on this tenant. Historical payment fixtures also need committed
                # commands visible to that process before capture. Their seed
                # helper owns the short transactions around provider I/O.
                tenant, owner = self.provisioner.owned(lease)
                self.validate_identity(owner, identity)
                self.mutate(lease, tenant, owner, action.operation)
                payment_fault = owner.get('payment_fault')
            with transaction.atomic():
                tenant, owner = self.provisioner.owned(lease, lock=True)
                if external_payment:
                    # Reload callback writes before completing the action ledger.
                    owner['payment_fault'] = payment_fault
                elif not external_fixture:
                    self.mutate(lease, tenant, owner, action.operation)
                after = self.evidence(tenant, owner)
                event.status = 'succeeded'
                event.detail = json.dumps({'before': before, 'after': after}, sort_keys=True)
                owner['action_ledger'][action.action_id].update(after=after, event=event.model_dump())
                self.provisioner.save_owner(tenant, owner)
        except BaseException as exc:
            # Never replay a possibly committed provider side effect automatically.
            tenant, owner = self.provisioner.owned(lease)
            event.status = 'blocked' if isinstance(exc, Blocked) else 'failed'
            event.detail = 'Action did not complete; owned resources and before evidence retained'
            owner['action_ledger'][action.action_id]['event'] = event.model_dump()
            owner['lifecycle'] = 'failed_preserved'
            self.provisioner.save_owner(tenant, owner)
            self.emit(event)
            raise
        self.emit(event)
        return event

    def validate_identity(self, owner, identity):
        if (owner['run_id'], owner['scenario_id'], owner['instance_id'], owner['attempt']) != (
                identity.run_id, identity.scenario_id, identity.scenario_instance_id, identity.attempt):
            raise Blocked('Action execution identity does not own this lease')
        if owner.get('plan_hash') != canonical_hash(self.plan.model_dump()):
            raise Blocked('Control plan differs from the plan used to provision this lease')

    def emit(self, event):
        if self.writer:
            self.writer.write(event)
            self.writer.flush()

    @staticmethod
    def active_chat(tenant, owner):
        # Anchor may roll over; select by exact customer/channel/browser identity.
        chat = om.ChatSession.objects.filter(tenant=tenant, customer_id=owner['maps']['customer:active'],
            platform='website', session_id=owner['browser_sessions'][0], is_completed=False).order_by('-created_at').first()
        if chat is None:
            raise Blocked('Action requires an active owned chat session')
        return chat

    def prerequisites(self, tenant, owner, op):
        if op.kind in ('catalog_control', 'set_delivery_fee', 'reconnect'):
            chat = self.active_chat(tenant, owner)
            draft = (chat.state or {}).get('checkout')
            if not draft or not draft.get('quote') or chat.order_id:
                raise Blocked('Action requires an active persisted quote before confirmation')
        if op.kind == 'catalog_control':
            variant = om.MenuItemVariant.objects.filter(menu_item__tenant=tenant,
                menu_item__name=op.item_name, size=op.variant_name)
            if variant.count() != 1:
                raise Blocked('Action requires one exact owned catalog variant')
            selected = [item for item in draft.get('basket', {}).get('items', [])
                        if item.get('item_variant_id') == str(variant.get().pk)]
            if not selected:
                raise Blocked('Reviewed catalog mutation requires the variant selected in the persisted quote')
            if op.price_minor is not None and variant.get().price != Decimal('460'):
                raise Blocked('Reviewed price-change precondition requires INR 460')
            if op.price_minor is not None and Decimal(str(draft['quote'].get('subtotal', '-1'))) != Decimal('920'):
                raise Blocked('Reviewed price change requires the INR 920 quote')
            if op.available is False and not variant.get().is_available:
                raise Blocked('Variant is already unavailable')
        if op.kind == 'set_delivery_fee':
            policy = om.CheckoutSettings.objects.get(tenant=tenant).configuration
            if Decimal(policy['modes']['delivery']['fee']) != Decimal('100'):
                raise Blocked('Reviewed fee-change precondition requires INR 100')
            if draft.get('mode') != 'delivery':
                raise Blocked('Reviewed fee change requires a delivery quote')
        if op.kind == 'payment_control' and op.operation in ('capture', 'fail', 'cancel'):
            payment = self.active_payment(tenant, owner)
            if payment.status != 'pending' or payment.currency != op.currency:
                raise Blocked('Payment event requires a matching pending payment')
            if op.amount_minor is not None and payment.requested_minor != op.amount_minor:
                raise Blocked('Payment amount does not match reviewed event')

    def active_payment(self, tenant, owner):
        chat = self.active_chat(tenant, owner)
        payments = cm.Payment.objects.filter(accepted_order__order_id=chat.order_id,
            accepted_order__order__tenant=tenant, accepted_order__order__customer_id=owner['maps']['customer:active'],
            connection__environment='test', connection__location__tenant=tenant)
        if not chat.order_id or payments.count() != 1:
            raise Blocked('Expected one active owned order and payment')
        return payments.get()

    def mutate(self, lease, tenant, owner, op):
        if op.kind == 'seed_fixture':
            apply_fixture(self.provisioner, lease, tenant, owner, get_fixture(op.fixture_id, op.fixture_hash))
        elif op.kind == 'freeze_clock':
            owner['clock'] = op.clock.model_dump()
        elif op.kind == 'lookup_control':
            if op.service == 'reverse_geocoding' and op.outcome != 'unavailable':
                raise Blocked('No reviewed successful reverse-geocode fixture exists')
            if op.service == 'classification' and op.outcome not in ('success', 'timeout'):
                raise Blocked('Unsupported classification fault')
            owner['lookup'][op.service] = op.outcome
            if op.service == 'geocoding':
                owner['geocoder_postal'] = op.postal_code if op.outcome == 'postal_mismatch' else None
        elif op.kind == 'catalog_control':
            variant = om.MenuItemVariant.objects.get(menu_item__tenant=tenant, menu_item__name=op.item_name, size=op.variant_name)
            if op.price_minor is not None:
                variant.price = Decimal(op.price_minor) / 100
            if op.available is not None:
                variant.is_available = op.available
            variant.save()
        elif op.kind == 'set_delivery_fee':
            settings = om.CheckoutSettings.objects.get(tenant=tenant)
            settings.configuration['modes']['delivery']['fee'] = str(Decimal(op.amount_minor) / 100)
            settings.full_clean()
            settings.save()
        elif op.kind == 'reconnect':
            store = SessionStore(session_key=owner['browser_sessions'][0])
            # Website state is a namespace in a database session. Retain its key
            # and customer binding, evict only ephemeral chat state. No Redis flush.
            store[owner['namespace']] = {'customer_id': owner['maps']['customer:active']}
            store.save()
        elif op.kind == 'payment_control':
            payment = self.active_payment(tenant, owner) if op.operation in ('capture', 'fail', 'cancel') else None
            self.provisioner.runtime.payment(lease, op.operation, payment)
            owner['payment_fault'] = {'timeout_creation': 'before_commit',
                                      'timeout_after_creation': 'after_commit'}.get(op.operation)
        else:
            raise Blocked('Unsupported typed operation')

    @staticmethod
    def evidence(tenant, owner):
        # Safe state projection: IDs/counts/hashes/configuration only. No contact,
        # cookies, API keys, signatures, payment URLs, or free-form ORM dumps.
        return dict(clock=owner['clock'], lookup=owner['lookup'],
            geocoder_postal=owner.get('geocoder_postal'), payment_fault=owner.get('payment_fault'),
            catalog={r.menu_item.name: {'price_minor': int(r.price * 100), 'available': r.is_available}
                 for r in om.MenuItemVariant.objects.filter(menu_item__tenant=tenant).select_related('menu_item')},
            checkout=om.CheckoutSettings.objects.get(tenant=tenant).configuration,
            addresses=om.CustomerAddress.objects.filter(tenant_id__in=owner['tenant_ids']).count(),
            sessions={str(r.pk): canonical_hash(r.state) for r in om.ChatSession.objects.filter(tenant_id__in=owner['tenant_ids'])},
            browser_state_hash=canonical_hash(SessionStore(session_key=owner['browser_sessions'][0]).get(owner['namespace'], {})),
            payments=[{'id': str(p.pk), 'status': p.status, 'requested_minor': p.requested_minor,
                       'captured_minor': p.captured_minor, 'currency': p.currency}
                      for p in cm.Payment.objects.filter(connection__location__tenant=tenant)],
            provider_events=[{'receipt_id': str(r.pk), 'type': r.event_type, 'status': r.status}
                             for r in cm.Inbox.objects.filter(connection__location__tenant=tenant)],
            orders=om.Order.objects.filter(tenant_id__in=owner['tenant_ids']).count(),
            branch_policies=deepcopy(owner['branch_policies']))
