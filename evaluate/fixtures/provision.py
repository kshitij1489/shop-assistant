"""Transactional tenant-per-scenario ORM provisioning and ownership-checked cleanup."""

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from django.db import transaction
from django.core.exceptions import ValidationError
from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.utils import timezone
from chatbot_core.models import TenantInfo, TenantJSONDoc, TenantRuntimeConfiguration
from chatbot_core.runtime_configuration import publish_configuration
from chatbot_core.scope import session_identity
from orders import models as om
from commerce import models as cm
from orders.checkout_config import CheckoutPolicy
from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.identity import canonical_hash
from evaluate.datasets.loader import read_json, contained
from chatbot_core.configuration_files import CONFIGURATION_FILES, knowledge_exports
from chatbot_core.configuration_imports import import_configuration, import_checkout
from evaluate.fixtures.definitions import ADDRESS_CASES, address
from evaluate.fixtures.capabilities import attest_publication, required_routes

OWNER_KEY = 'evaluation_owned_v1'
FOREIGN_ADDRESS_ID = '11111111-1111-4111-8111-111111111111'


def checkout_policy(setup, root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2] / 'test_data'
    config = read_json(contained(root, CONFIGURATION_FILES['checkout']))
    assumptions = read_json(contained(root, 'evaluation_setup.json'))
    modes = setup.modes if setup.modes is not None else assumptions['default_modes']
    config['modes'] = {mode: deepcopy(config['modes'][mode]) for mode in modes}
    for mode, policy in config['modes'].items():
        required = setup.required_delivery if mode == 'delivery' else setup.required_pickup if mode == 'pickup' else None
        if required is not None:
            policy['required_fields'] = required
        if setup.payment_methods is not None and mode != 'dine_in':
            policy['payment_methods'] = setup.payment_methods
        for field, value in (('preparation_minutes', setup.preparation_minutes),
                             ('max_advance_days', setup.horizon_days)):
            if value is not None:
                policy[field] = value
        if setup.scheduling is not None and mode == 'pickup':
            policy['scheduling_enabled'] = setup.scheduling
        for field, value in (('minimum_order', setup.delivery_minimum_minor if mode == 'delivery' else setup.pickup_minimum_minor),
                             ('fee', setup.delivery_fee_minor if mode == 'delivery' else setup.pickup_fee_minor)):
            if value is not None:
                policy[field] = str(Decimal(value) / 100)
    if setup.allowed_postal_codes is not None:
        config['delivery_postal_codes'] = setup.allowed_postal_codes
    if setup.payment == 'fake_adapter':
        config['online_provider'] = 'adapter'
    return CheckoutPolicy.model_validate(config).model_dump(mode='json')


class DjangoProvisioner:
    """Inject a runtime/control lane implementing prepare/capabilities/release.

    Binding is private (browser cookies and credentials must never enter artifacts).
    inspect() returns a safe provisioning manifest. Failed scenarios are retained
    by finish(); only confirmed success or cleanup(force=True) deletes resources.
    """
    def __init__(self, runtime=None):
        self.runtime = runtime
        if runtime is not None:
            runtime.provisioner = self

    def provision(self, config, scenario, identity):
        from evaluate.scenarios.plan import load_plan
        plan, bundle = load_plan(config.dataset_directory)
        reviewed = next((s for s in bundle.scenarios if s.scenario_id == scenario.scenario_id), None)
        if reviewed != scenario:
            raise Blocked('Scenario differs from the validated reviewed dataset plan')
        if scenario.blockers:
            raise Blocked('Scenario has unresolved normalization blockers')
        if (config.run_id != identity.run_id or identity.scenario_id != scenario.scenario_id
                or config.scenario_plan_version != scenario.scenario_plan_version):
            raise Blocked('Configuration, scenario and execution identities disagree')
        if self.runtime is None:
            raise Blocked('An evaluation runtime lane is required for clock and lookup isolation')
        if set(plan.supported_capabilities) - self.runtime.capabilities():
            raise Blocked('Runtime lane does not implement the reviewed plan capabilities')
        self.runtime.attest(scenario)
        lease = Lease(str(uuid4()), identity.scenario_instance_id)
        try:
            with transaction.atomic():
                # Every attempt gets a new UUID; retries never adopt old resources.
                slug = 'eval-' + uuid4().hex
                owner = dict(lease=lease.handle, instance_id=lease.scenario_instance_id,
                             run_id=identity.run_id, scenario_id=identity.scenario_id,
                             attempt=identity.attempt, lifecycle='provisioning',
                             dataset_hash=scenario.source_hash, plan_hash=canonical_hash(plan.model_dump()),
                             action_ledger={}, branch_policies=[],
                             maps={}, tenant_ids=[], browser_sessions=[], setup=scenario.setup.model_dump(),
                             clock=scenario.clock.model_dump(), lookup={}, original_turn_index=None)
                tenant = TenantInfo.objects.create(slug=slug, display_name=slug,
                    approval_status='APPROVED', reviewed_at=timezone.now(),
                    review_note='Evaluation-owned synthetic tenant', allowed_domains=[config.base_url],
                    meta={OWNER_KEY: owner})
                owner['tenant_ids'].append(tenant.pk)
                customer = om.Customer.objects.create(tenant=tenant, name='QA Guest', phone='')
                browser = SessionStore()
                browser.create()
                namespace = 'cafe:v2:' + session_identity(str(tenant.pk), 'website', browser.session_key)
                browser[namespace] = {'customer_id': str(customer.pk), 'basket': {'items': []}}
                browser.save()
                chat = om.ChatSession.objects.create(tenant=tenant, customer=customer,
                    platform='website', session_id=browser.session_key, state={})
                owner['browser_sessions'].append(browser.session_key)
                owner['namespace'] = namespace
                owner['browser_namespaces'] = {browser.session_key: namespace}
                owner['maps'] = {'tenant': str(tenant.pk), 'customer:active': str(customer.pk),
                                 'session:active': str(chat.pk), 'catalog': {}, 'addresses': {},
                                 'connections': {}, 'orders': {}, 'categories': {}, 'stock': {}, 'knowledge': {}}
                root = Path(config.dataset_directory)
                assumptions = read_json(contained(root, 'evaluation_setup.json'))
                commerce = self.import_configuration(tenant, root,
                    catalog=scenario.setup.catalog == 'published_synthetic')
                for item in om.MenuItem.objects.filter(tenant=tenant).select_related('category_fk').prefetch_related('variants'):
                    variants = list(item.variants.all())
                    label = scenario.setup.variant_name or assumptions['variant_name']
                    variant = next((v for v in variants if v.size == label), None)
                    if variant is None:
                        raise Blocked('Declared scenario variant is absent from the canonical catalog')
                    owner['maps']['catalog'][item.name] = {'item': str(item.pk), 'variant': str(variant.pk)}
                    if item.category_fk:
                        owner['maps']['categories'][item.category_fk.name] = str(item.category_fk_id)
                location = commerce.location
                commerce_enabled = scenario.setup.payment == 'fake_adapter'
                commerce.enabled = commerce_enabled
                commerce.save(update_fields=['enabled'])
                owner['maps'].update(location=str(location.pk), commerce_configuration=str(commerce.pk))
                if commerce_enabled:
                    for role, capabilities in assumptions['payment_connections'].items():
                        connection = cm.Connection.objects.create(location=location, role=role, provider='custom',
                            account_id=str(uuid4()), environment='test', active=True,
                            secret_ref='managed:' + uuid4().hex, capabilities=capabilities,
                            metadata={'evaluation_owned': lease.handle})
                        owner['maps']['connections'][role] = str(connection.pk)
                if scenario.setup.stock == 'finite_local':
                    for name, ids in owner['maps']['catalog'].items():
                        stock = cm.StockItem.objects.create(location=location, variant_id=ids['variant'],
                            mode='quantity', on_hand=assumptions['quantity_per_variant'], observed_at=timezone.now())
                        owner['maps']['stock'][name] = str(stock.pk)
                settings = import_checkout(tenant, checkout_policy(scenario.setup, root))
                owner['maps']['checkout_settings'] = str(settings.pk)
                pins = set(scenario.setup.allowed_postal_codes or [])
                if scenario.setup.address_lookup == 'complete_supplied_only':
                    if scenario.source_id not in ADDRESS_CASES:
                        raise Blocked('Missing reviewed complete-address fixtures')
                    pins.update(address(k)['components']['postal_code'] for k in ADDRESS_CASES[scenario.source_id])
                if pins:
                    tenant.meta['serviceable_pincodes'] = sorted(pins)
                owner['address_keys'] = ADDRESS_CASES.get(scenario.source_id, [])
                owner['lookup'] = {'coverage': 'success' if pins else 'unavailable',
                    'geocoding': 'success' if scenario.setup.address_lookup == 'complete_supplied_only' else 'unavailable',
                    'reverse_geocoding': 'unavailable', 'classification': 'success'}
                owner['assumptions'] = {
                    'variant': assumptions['variant_name'],
                    'scenario_overrides': scenario.setup.model_dump(exclude_none=True),
                    'stock': {'kind': scenario.setup.stock or 'none',
                              'quantity_per_variant': assumptions['quantity_per_variant'] if scenario.setup.stock == 'finite_local' else None,
                              'public_fact': assumptions['public_stock_fact']},
                    'contact': assumptions['contact'],
                    'coverage_and_fees_public_fact': assumptions['coverage_and_fees_public_fact'],
                }
                self.save_owner(tenant, owner)
                owner['capabilities'] = self.publish(tenant, scenario, Path(config.dataset_directory))
                owner['maps']['knowledge'] = {r.intent + '/' + r.sub_intent: str(r.pk)
                    for r in TenantJSONDoc.objects.filter(tenant=tenant, dtype='knowledge')}
                self.save_owner(tenant, owner)
                self.runtime.prepare(lease, self.binding(lease), scenario)
                owner['lifecycle'] = 'ready'
                self.save_owner(tenant, owner)
        except BaseException:
            self.runtime.release(lease)
            self.runtime.cleanup_files(lease)
            raise
        return lease

    @staticmethod
    def save_owner(tenant, owner):
        tenant.meta = {**(tenant.meta or {}), OWNER_KEY: deepcopy(owner)}
        tenant.save(update_fields=['meta'])

    @staticmethod
    @transaction.atomic
    def import_configuration(tenant, root, *, catalog=True):
        root = Path(root)
        exports = knowledge_exports(root)
        for filename, generated in exports.items():
            if read_json(contained(root, filename)) != generated:
                raise Blocked(f'Derived knowledge is stale: {filename}')
        commerce = import_configuration(tenant, 'commerce_policy',
            contained(root, CONFIGURATION_FILES['commerce_policy']).read_text())
        if catalog:
            import_configuration(tenant, 'catalog', contained(root, CONFIGURATION_FILES['catalog']).read_text())
        import_configuration(tenant, 'knowledge', {'document_type': 'knowledge', 'documents': exports['knowledge_base.json']})
        for kind in ('intent_classification', 'response_intents', 'checkout'):
            source = read_json(contained(root, CONFIGURATION_FILES[kind]))
            if kind != 'checkout' and 'document_type' not in source:
                source = {'document_type': kind, 'documents': source}
            import_configuration(tenant, kind, source)
        return commerce

    @staticmethod
    def publish(tenant, scenario, root):
        knowledge = set(TenantJSONDoc.objects.filter(tenant=tenant, dtype='knowledge')
                        .values_list('intent', 'sub_intent'))
        routes = required_routes(scenario, knowledge)
        try:
            publication = publish_configuration(tenant.pk, expected_version=0)
        except ValidationError as exc:
            raise Blocked('Fixture capability configuration invalid: ' + '; '.join(exc.messages)) from exc
        return attest_publication(tenant, publication, routes)

    def owned(self, lease, *, lock=False):
        query = TenantInfo.objects.select_for_update() if lock else TenantInfo.objects
        # The UUID is an ownership marker, never a tenant slug prefix authorization.
        matches = query.filter(meta__evaluation_owned_v1__lease=lease.handle,
                               meta__evaluation_owned_v1__instance_id=lease.scenario_instance_id)
        roots = [t for t in matches if (t.meta[OWNER_KEY].get('maps') or {}).get('tenant') == str(t.pk)]
        if len(roots) != 1:
            raise Blocked('Owned evaluation lease is missing or ambiguous')
        return roots[0], deepcopy(roots[0].meta[OWNER_KEY])

    def binding(self, lease):
        tenant, owner = self.owned(lease)
        return dict(tenant=tenant, customer=om.Customer.objects.get(pk=owner['maps']['customer:active'], tenant=tenant),
                    chat=om.ChatSession.objects.get(pk=owner['maps']['session:active'], tenant=tenant),
                    browser_session=owner['browser_sessions'][0], namespace=owner['namespace'])

    def inspect(self, lease):
        # Inspect is DB-only so it still succeeds after a preserved attempt whose
        # runtime workers were released. Shape stays stable for runner artifacts.
        tenant, owner = self.owned(lease)
        publication = TenantRuntimeConfiguration.objects.get(tenant=tenant)
        return dict(schema_version='1.0.0', lease_id=lease.handle, run_id=owner['run_id'],
            scenario_id=owner['scenario_id'], scenario_instance_id=owner['instance_id'], attempt=owner['attempt'],
            lifecycle=owner['lifecycle'], dataset_hash=owner['dataset_hash'], plan_hash=owner['plan_hash'], identities=owner['maps'],
            message_bindings=owner.get('message_bindings', {}),
            owned_tenant_ids=owner['tenant_ids'], assumptions=owner['assumptions'], setup=owner['setup'],
            runtime={'mode': 'in_process', 'parallel_workers': 'separate_processes',
                     'customer_binding': 'owned_server_session'},
            publication={'version': publication.version, 'documents': len(publication.documents),
                         'hash': canonical_hash(publication.documents),
                         'capabilities': owner.get('capabilities')},
            branch_policies=owner['branch_policies'], actions=owner['action_ledger'])

    def finish(self, lease, *, succeeded):
        if succeeded:
            self.cleanup(lease, force=True)
            return
        # Preserve failed attempts for inspect(); do not delete owned rows.
        tenant, owner = self.owned(lease)
        owner['lifecycle'] = 'failed_preserved'
        self.save_owner(tenant, owner)
        if self.runtime:
            self.runtime.release(lease)

    def cleanup(self, lease, *, force=False):
        # The shared runner can call cleanup unconditionally without supplying
        # an outcome. Preserve unknown/failed attempts by default; only finish
        # with confirmed success or an explicit operator cleanup deletes rows.
        # Runtime workers are released on preserve, but ownership rows and
        # provider receipts remain so inspect(lease) still works afterward.
        if not force:
            if not TenantInfo.objects.filter(meta__evaluation_owned_v1__lease=lease.handle).exists():
                return
            tenant, owner = self.owned(lease)
            if owner['lifecycle'] != 'failed_preserved':
                owner['lifecycle'] = 'preserved_unconfirmed'
            self.save_owner(tenant, owner)
            if self.runtime:
                self.runtime.release(lease)
            return
        if self.runtime:
            self.runtime.validate_files(lease)
        with transaction.atomic():
            if not TenantInfo.objects.filter(meta__evaluation_owned_v1__lease=lease.handle).exists():
                if self.runtime:
                    self.runtime.release(lease)
                    self.runtime.cleanup_files(lease)
                return
            tenant, owner = self.owned(lease, lock=True)
            default_tenant_ids = owner['tenant_ids']
            tenants = list(TenantInfo.objects.select_for_update().filter(pk__in=default_tenant_ids))
            if len(tenants) != len(default_tenant_ids) or any(
                (t.meta or {}).get(OWNER_KEY, {}).get('lease') != lease.handle or
                (t.meta or {}).get(OWNER_KEY, {}).get('instance_id') != lease.scenario_instance_id for t in tenants):
                raise Blocked('Cleanup ownership check failed')
            queries = cleanup_queries(default_tenant_ids)
            assert_no_foreign_references(queries)
            # Verify session namespaces before deleting the dedicated browser row.
            for key in owner['browser_sessions']:
                row = Session.objects.filter(session_key=key).first()
                if row:
                    data = row.get_decoded()
                    if set(data) - {owner['browser_namespaces'][key]}:
                        raise Blocked('Browser session contains unrelated data; retaining lease')
            if self.runtime:
                self.runtime.release(lease)
            for query in queries:
                query.delete()
            Session.objects.filter(session_key__in=owner['browser_sessions']).delete()
        if self.runtime:
            self.runtime.cleanup_files(lease)

def cleanup_queries(ids):
    """Dependency order is explicit; protected commerce rows cannot cascade out."""
    return [
        cm.Inbox.objects.filter(connection__location__tenant_id__in=ids),
        cm.Command.objects.filter(connection__location__tenant_id__in=ids),
        cm.ReconciliationIssue.objects.filter(accepted_order__location__tenant_id__in=ids),
        cm.Reservation.objects.filter(accepted_order__location__tenant_id__in=ids),
        cm.Payment.objects.filter(accepted_order__location__tenant_id__in=ids),
        cm.ExternalMapping.objects.filter(connection__location__tenant_id__in=ids),
        cm.AcceptedOrder.objects.filter(location__tenant_id__in=ids),
        cm.StockItem.objects.filter(location__tenant_id__in=ids),
        cm.MenuSource.objects.filter(tenant_id__in=ids),
        cm.Configuration.objects.filter(tenant_id__in=ids),
        cm.Connection.objects.filter(location__tenant_id__in=ids),
        cm.Location.objects.filter(tenant_id__in=ids),
        om.OrderItemAddon.objects.filter(order_item__order__tenant_id__in=ids),
        om.OrderItem.objects.filter(order__tenant_id__in=ids),
        om.ChatSession.objects.filter(tenant_id__in=ids),
        om.Order.objects.filter(tenant_id__in=ids),
        om.CustomerAddress.objects.filter(tenant_id__in=ids),
        om.Customer.objects.filter(tenant_id__in=ids),
        om.VariantTaxMap.objects.filter(variant__menu_item__tenant_id__in=ids),
        om.ItemAddonGroup.objects.filter(tenant_id__in=ids),
        om.AddonItem.objects.filter(group__tenant_id__in=ids),
        om.AddonGroup.objects.filter(tenant_id__in=ids),
        om.MenuCatalogMeta.objects.filter(menu_item__tenant_id__in=ids),
        om.MenuItemVariant.objects.filter(menu_item__tenant_id__in=ids),
        om.MenuItem.objects.filter(tenant_id__in=ids), om.MenuCategory.objects.filter(tenant_id__in=ids),
        om.Tax.objects.filter(tenant_id__in=ids), om.DeliveryPartner.objects.filter(tenant_id__in=ids),
        om.PlatformWebhookLog.objects.filter(tenant_id__in=ids), om.CheckoutSettings.objects.filter(tenant_id__in=ids),
        TenantJSONDoc.objects.filter(tenant_id__in=ids), TenantRuntimeConfiguration.objects.filter(tenant_id__in=ids),
        TenantInfo.objects.filter(pk__in=ids),
    ]


def assert_no_foreign_references(queries):
    from django.apps import apps
    owned = {q.model: set(q.values_list('pk', flat=True)) for q in queries}
    for model in apps.get_models():
        for field in model._meta.fields:
            target = field.remote_field.model if field.is_relation and field.remote_field else None
            if target in owned and owned[target]:
                refs = model.objects.filter(**{field.attname + '__in': owned[target]})
                if refs.exclude(pk__in=owned.get(model, set())).exists():
                    raise Blocked('Unrelated rows reference evaluation resources; cleanup refused')
