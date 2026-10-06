from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.test import TestCase, override_settings
from django.contrib.sessions.backends.db import SessionStore
from chatbot_core.models import TenantInfo, TenantRuntimeConfiguration
from orders import models as om
from commerce import models as cm
from evaluate.contracts.models import RunConfiguration, ExecutionIdentity, ModelNames, ScenarioPlan
from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.datasets.loader import load_dataset, read_json, resolve_profiles
from evaluate.identity import instance_id
from evaluate.fixtures.provision import DjangoProvisioner, FOREIGN_ADDRESS_ID
from evaluate.scenarios.runtime import LocalRuntime
from evaluate.scenarios.controls import DatasetControls
from mock_services.catalog import load_catalog
from mock_services.client import MockClient
from mock_services.controls import ProviderControls
from mock_services.server import make_server
import threading

ROOT = Path(__file__).resolve().parents[2]


class ProvisionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = ScenarioPlan.model_validate(read_json(ROOT / 'scenarios/execution.plan.json'))
        cls.bundle = load_dataset(ROOT.parent / 'test_data', cls.plan)
        cls.cases = {s.source_id: s for s in cls.bundle.scenarios}
        cls.emulator_tmp = TemporaryDirectory()
        cls.emulator = make_server(('127.0.0.1', 0), Path(cls.emulator_tmp.name) / 'location.sqlite3',
                                   load_catalog())
        cls.emulator_thread = threading.Thread(target=cls.emulator.serve_forever, daemon=True)
        cls.emulator_thread.start()
        cls.emulator_url = 'http://127.0.0.1:%s' % cls.emulator.server_port

    @classmethod
    def tearDownClass(cls):
        cls.emulator.shutdown()
        cls.emulator.server_close()
        cls.emulator_thread.join(timeout=5)
        cls.emulator_tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        from tests.support.replies import install_reply_renderer
        install_reply_renderer(self)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = LocalRuntime(self.temp.name)
        self.provisioner = DjangoProvisioner(self.runtime)
        self.runtime.provisioner = self.provisioner
        self.controls = DatasetControls(self.provisioner, self.plan)
        self.config = RunConfiguration(run_id='local-tests', scenario_plan_version=self.plan.version,
            dataset_directory=str(ROOT.parent / 'test_data'), models=ModelNames(chat='unused', translate='unused', analytics='unused'))
        self.settings_override = override_settings(
            LOCATION_PROVIDER='emulator', LOCATION_EMULATOR_URL=self.emulator_url,
            LOCATION_EMULATOR_TIMEOUT_SECONDS=2.0)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.location = ProviderControls(MockClient(self.emulator_url))

    def provision(self, source, setup=True):
        scenario = self.cases[source]
        identity = ExecutionIdentity(run_id=self.config.run_id, scenario_id=scenario.scenario_id,
            scenario_instance_id=instance_id(self.config.run_id, scenario.scenario_id, 0), attempt=1)
        lease = self.provisioner.provision(self.config, scenario, identity)
        self.addCleanup(self.runtime.release, lease)
        if setup:
            self.controls.before_turn(lease, identity, scenario, None)
        return lease, identity, scenario

    def test_complete_mapping_and_preserved_indexes(self):
        self.assertEqual(len(self.plan.scenarios), 219)
        self.assertEqual([s.source_id for s in self.bundle.blocked], [])
        case = self.cases['s122_delivery_online_end_to_end']
        self.assertEqual(case.actions[0].original_turn_index, 20)
        self.assertEqual(case.actions[0].operation.amount_minor, 102000)
        self.assertEqual(len([s for s in self.bundle.scenarios if s.namespace == 'qa']), 19)
        for s in self.bundle.scenarios:
            self.assertFalse(any(b.code == 'unmapped_requirement' for b in s.blockers))

    @override_settings(EVALUATION_ENABLED=True, EVALUATION_LOCATION_PROVIDER='emulator')
    def test_s102_fresh_session_supports_unchanged_basket_rejection(self):
        from evaluate.checks.engine import DeterministicEvaluator
        from evaluate.checks.plan_checks import generate_check_specs
        from evaluate.controls.inspection import StateInspector
        from evaluate.reports.example import synthetic_run

        source = next(name for name in self.cases if name.startswith('s102_'))
        lease, identity, scenario = self.provision(source)
        inspector = StateInspector(self.provisioner)
        before = inspector.snapshot(lease, identity, 0, 'rejection', 'before')
        self.assertEqual(before.state['basket'], {'items': []})
        self.assertNotIn('basket', before.unavailable_sections)
        binding = self.provisioner.binding(lease)
        browser = SessionStore(session_key=binding['browser_session'])
        data = browser[binding['namespace']]
        # The handler persists an empty basket and clears rejected ordering work.
        data.update(basket={'items': []}, ongoing_query_queue=[])
        browser[binding['namespace']] = data
        browser.save()
        after = inspector.snapshot(lease, identity, 0, 'rejection', 'after')
        turn = synthetic_run().turns[0].model_copy(update={
            **identity.model_dump(), 'original_turn_index': 0, 'request_id': 'rejection'})
        checks = [c for c in generate_check_specs([scenario])
                  if c.original_turn_index == 0 and ':reviewed:' in c.check_id]
        self.assertTrue(checks)
        for check in checks:
            with self.subTest(kind=check.kind, path=check.path):
                result = DeterministicEvaluator().evaluate(check, turn, [before, after])
                self.assertEqual(result.outcome, 'PASS', result.explanation)

    def test_profile_precedence(self):
        a, _ = resolve_profiles(['checkout_sandbox', 'catalog_sandbox'], self.plan)
        b, _ = resolve_profiles(['catalog_sandbox', 'checkout_sandbox'], self.plan)
        self.assertEqual(a, b)
        self.assertEqual(a.payment, 'fake_adapter')
        self.assertEqual(a.stock, 'finite_local')
        self.assertEqual(self.cases['s133_schedule_validation'].setup.scheduling, True)

    def test_address_save_publishes_confirmation(self):
        lease, _, _ = self.provision('s06_save_address')
        tenant = self.provisioner.binding(lease)['tenant']
        docs = TenantRuntimeConfiguration.objects.get(tenant=tenant).documents
        confirm = [d for d in docs if d['intent'] == 'location_based' and d['sub_intent'] == 'confirm_delivery_address']
        self.assertEqual({d['dtype'] for d in confirm}, {'intent_classification', 'response_intents'})
        classification = next(d for d in confirm if d['dtype'] == 'intent_classification')
        self.assertTrue(classification['payload']['enabled'])
        fixture = read_json(ROOT.parent / 'test_data/intent_classification.json')
        self.assertEqual(classification['payload']['description'],
                         fixture['location_based']['confirm_delivery_address'])
        basket, _, _ = self.provision('s01_add_pistachio')
        basket_docs = TenantRuntimeConfiguration.objects.get(
            tenant=self.provisioner.binding(basket)['tenant']).documents
        self.assertFalse(any(d['sub_intent'] == 'confirm_delivery_address' for d in basket_docs))

    def scripted_turn(self, lease, identity, intent, topic, action=None):
        """Exercise real fixture permissions and handlers with only understanding scripted."""
        from chatbot_core.llm.schemas import ClassifiedMessages, IntentClassification
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore, _session_data
        from chatbot_core.logic.cafe.workflow import graph, runner
        from evaluate.controls.context import activate
        from evaluate.controls.ownership import context_for
        binding = self.provisioner.binding(lease)
        store = MemorySessionStore(binding['browser_session'], tenant_id=binding['tenant'].pk,
                                   platform='website')
        self.addCleanup(_session_data.clear)
        proposal = ClassifiedMessages(declared_constraints=[], classifications=[IntentClassification(
            query='Test request', intent=intent, sub_intent=topic, reply_to=None,
            clarification=None, action=action)])
        context, _ = context_for(lease, identity, 'capability-test', self.provisioner)
        with activate(context), self.runtime.turn(lease), \
                patch.object(graph, 'normalize_and_classify', return_value=proposal), \
                patch.object(graph, 'enqueue_string'), patch.object(runner, 'enqueue_string'), \
                self.assertLogs('evaluate.telemetry', 'INFO') as logs:
            reply, _ = runner.run_conversation(binding['tenant'], store, 'Test request', binding['customer'])
        decisions = [r.evaluation for r in logs.records if r.evaluation['event'] == 'capability.checked']
        self.assertEqual(len(decisions), 1)
        return reply, decisions[0]

    @override_settings(EVALUATION_ENABLED=True, EVALUATION_LOCATION_PROVIDER='emulator')
    def test_address_listing_and_wrong_cart_action_have_distinct_evidence(self):
        from chatbot_core.llm.schemas import ActionProposal
        lease, identity, _ = self.provision('s36_addresses_default_and_map_pin')
        contract = self.provisioner.inspect(lease)['publication']['capabilities']
        self.assertIn(['location_based', 'existing_addresses'], contract['required_routes'])
        self.assertNotIn(['placing_order', 'check_order_cart'], contract['enabled_routes'])
        reply, decision = self.scripted_turn(lease, identity, 'location_based', 'existing_addresses')
        self.assertIn('Work', reply)
        self.assertIn('Home', reply)
        self.assertEqual(decision['status'], 'allowed')
        reply, decision = self.scripted_turn(lease, identity, 'location_based', 'existing_addresses',
                                            ActionProposal(kind='SHOW_CART'))
        self.assertIn('currently unavailable', reply)
        self.assertEqual(decision['classified_route'], ['location_based', 'existing_addresses'])
        self.assertEqual(decision['effective_route'], ['placing_order', 'check_order_cart'])
        self.assertEqual(decision['unavailable_routes'], [['placing_order', 'check_order_cart']])
        self.assertEqual(decision['action_kind'], 'SHOW_CART')
        self.assertEqual(decision['configuration_version'], 1)
        self.assertFalse(om.Order.objects.exists())

    @override_settings(EVALUATION_ENABLED=True, EVALUATION_LOCATION_PROVIDER='emulator')
    def test_order_start_can_dispatch_to_checkout_without_fixture_gap(self):
        from chatbot_core.llm.schemas import ActionProposal
        lease, identity, _ = self.provision('s02_start_an_order')
        contract = self.provisioner.inspect(lease)['publication']['capabilities']
        self.assertIn(['placing_order', 'order_confirmation'], contract['required_routes'])
        self.assertIn(['placing_order', 'add_to_basket'], contract['enabled_routes'])
        reply, decision = self.scripted_turn(lease, identity, 'placing_order', 'initiate_order',
                                            ActionProposal(kind='CONTINUE_CHECKOUT'))
        # This fixture has no basket. Capability dispatch succeeds, but checkout
        # must still enforce its business precondition before collecting a name.
        self.assertIn('Your basket is empty', reply)
        self.assertFalse(om.Order.objects.exists())
        self.assertEqual(decision['classified_route'], ['placing_order', 'initiate_order'])
        self.assertEqual(decision['effective_route'], ['placing_order', 'order_confirmation'])
        self.assertEqual(decision['unavailable_routes'], [])
        self.assertEqual(decision['status'], 'allowed')

    def test_missing_capabilities_block_setup_and_roll_back_before_runtime(self):
        from evaluate.fixtures import provision
        publish = provision.publish_configuration
        count = TenantInfo.objects.count()
        for defect, message in [('route', 'Fixture capability coverage missing'),
                                ('settings', 'Fixture capability configuration invalid')]:
            def incomplete_publication(tenant_id, **kwargs):
                if defect == 'settings':
                    om.CheckoutSettings.objects.filter(tenant_id=tenant_id).delete()
                publication = publish(tenant_id, **kwargs)
                if defect == 'route':
                    publication.documents = [d for d in publication.documents
                        if (d['intent'], d['sub_intent']) != ('placing_order', 'order_confirmation')]
                    publication.save(update_fields=['documents'])
                return publication
            with self.subTest(defect=defect), \
                    patch.object(provision, 'publish_configuration', side_effect=incomplete_publication), \
                    patch.object(self.runtime, 'prepare') as prepare:
                with self.assertRaisesRegex(Blocked, message):
                    self.provision('s02_start_an_order')
                prepare.assert_not_called()
                self.assertEqual(TenantInfo.objects.count(), count)
                self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_publication_and_no_live_knowledge_data(self):
        lease, _, _ = self.provision('stock')
        b = self.provisioner.binding(lease)
        tenant = b['tenant']
        self.assertFalse(om.MenuItem.objects.filter(tenant=tenant).exists())
        self.assertFalse(cm.Connection.objects.filter(location__tenant=tenant).exists())
        self.assertFalse(om.Order.objects.filter(tenant=tenant).exists())
        docs = TenantRuntimeConfiguration.objects.get(tenant=tenant).documents
        self.assertEqual(sum(d['dtype'] == 'knowledge' for d in docs), 33)
        self.assertTrue(any(d['intent'] == 'menu_items' and d['sub_intent'] == 'availability' and d['dtype'] == 'intent_classification' for d in docs))
        manifest = self.provisioner.inspect(lease)
        rendered = str(manifest)
        self.assertNotIn(tenant.api_key, rendered)
        self.assertNotIn(b['browser_session'], rendered)

    def test_scenario_and_attempt_isolation_stock_resolution(self):
        basket, _, _ = self.provision('s01_add_pistachio')
        checkout, identity, s = self.provision('s122_delivery_online_end_to_end')
        another = self.provisioner.provision(self.config, s, identity.model_copy(update={'attempt': 2}))
        self.addCleanup(self.runtime.release, another)
        first = self.provisioner.binding(basket)['tenant']
        second = self.provisioner.binding(checkout)['tenant']
        self.assertNotEqual(second.pk, self.provisioner.binding(another)['tenant'].pk)
        self.assertFalse(cm.StockItem.objects.filter(location__tenant=first).exists())
        self.assertEqual(cm.StockItem.objects.filter(location__tenant=second).count(), 26)
        self.assertFalse(om.MenuItemVariant.objects.filter(menu_item__tenant=second).exclude(size='QA standard').exists())
        self.assertFalse(self.provisioner.inspect(checkout)['assumptions']['stock']['public_fact'])

    def test_saved_addresses_and_canary(self):
        lease, _, _ = self.provision('s119_foreign_address_id')
        tenant, owner = self.provisioner.owned(lease)
        own = om.CustomerAddress.objects.get(tenant=tenant)
        foreign_id = owner['maps']['addresses']['foreign']
        foreign = om.CustomerAddress.objects.get(pk=foreign_id)
        self.assertNotEqual(own.tenant_id, foreign.tenant_id)
        self.assertNotEqual(own.customer_id, foreign.customer_id)
        self.assertEqual(own.components['postal_code'], '122102')
        self.assertEqual(foreign.label, 'CANARY')
        self.provisioner.cleanup(lease, force=True)
        self.assertFalse(om.CustomerAddress.objects.filter(pk=foreign_id).exists())

    def test_foreign_address_force_cleanup_allows_repetition(self):
        first, _, _ = self.provision('s119_foreign_address_id')
        self.provisioner.finish(first, succeeded=False)
        manifest = self.provisioner.inspect(first)
        self.assertEqual(manifest['lifecycle'], 'failed_preserved')
        self.assertEqual(manifest['lease_id'], first.handle)
        self.assertIn('tenant', manifest['identities'])
        second, _, _ = self.provision('s119_foreign_address_id')
        first_id = manifest['identities']['addresses']['foreign']
        second_id = self.provisioner.inspect(second)['identities']['addresses']['foreign']
        self.assertNotEqual(first_id, second_id)
        self.assertTrue(om.CustomerAddress.objects.filter(pk=first_id).exists())
        self.assertTrue(om.CustomerAddress.objects.filter(pk=second_id).exists())
        self.provisioner.cleanup(first, force=True)
        self.assertTrue(om.CustomerAddress.objects.filter(pk=second_id).exists())
        self.provisioner.cleanup(second, force=True)

    def test_address_provision_does_not_need_location_emulator(self):
        for source in ('s06_save_address', 's31_wrong_address_then_fix'):
            with self.subTest(source=source), override_settings(LOCATION_EMULATOR_URL='http://127.0.0.1:1'):
                lease, _, _ = self.provision(source, setup=False)
            self.assertIsNotNone(self.provisioner.binding(lease)['customer'])

    def test_failed_run_preserved_and_cleanup_safe(self):
        unrelated = TenantInfo.objects.create(slug='unrelated', display_name='Unrelated')
        lease, _, _ = self.provision('s122_delivery_online_end_to_end')
        self.provisioner.finish(lease, succeeded=False)
        self.assertEqual(self.provisioner.inspect(lease)['lifecycle'], 'failed_preserved')
        wrong = Lease(lease.handle, 'wrong-instance')
        with self.assertRaises(Blocked):
            self.provisioner.cleanup(wrong, force=True)
        self.provisioner.cleanup(lease, force=True)
        self.provisioner.cleanup(lease, force=True)
        self.assertTrue(TenantInfo.objects.filter(pk=unrelated.pk).exists())

    def test_cleanup_refuses_cross_tenant_references(self):
        lease, _, _ = self.provision('s01_add_pistachio')
        b = self.provisioner.binding(lease)
        other = TenantInfo.objects.create(slug='other', display_name='Other')
        order = om.Order.objects.create(tenant=other, customer=b['customer'], source='website', total_amount=0)
        with self.assertRaises(Blocked):
            self.provisioner.cleanup(lease, force=True)
        self.assertTrue(om.Order.objects.filter(pk=order.pk).exists())
        self.assertTrue(TenantInfo.objects.filter(pk=b['tenant'].pk).exists())

    def test_price_action_prerequisites_dedup_evidence(self):
        lease, identity, s = self.provision('s129_checkout_price_changed')
        with self.assertRaises(Blocked):
            self.controls.before_turn(lease, identity, s, 12)
        b = self.provisioner.binding(lease)
        from evaluate.fixtures.seeds import basket
        _, owner = self.provisioner.owned(lease)
        b['chat'].state = {'checkout': {'quote': {'subtotal': '920', 'total': '920'},
                                       'basket': basket(owner, {'Pistachio Ice Cream': 2})}}
        b['chat'].save()
        event, = self.controls.before_turn(lease, identity, s, 12)
        self.assertEqual(event.status, 'succeeded')
        self.assertEqual(self.controls.before_turn(lease, identity, s, 12)[0], event)
        evidence = self.provisioner.inspect(lease)['actions'][s.actions[0].action_id]
        self.assertEqual(evidence['before']['catalog']['Pistachio Ice Cream']['price_minor'], 46000)
        self.assertEqual(evidence['after']['catalog']['Pistachio Ice Cream']['price_minor'], 47000)

    def test_lookup_clock_and_settings_actions(self):
        lease2, identity2, s2 = self.provision('s144_closing_revalidation')
        self.controls.before_turn(lease2, identity2, s2, 12)
        self.assertIn('23:20', self.provisioner.owned(lease2)[1]['clock']['at'])
        lease3, _, _ = self.provision('s139_dine_in_end_to_end')
        policy = om.CheckoutSettings.objects.get(tenant=self.provisioner.binding(lease3)['tenant']).configuration
        self.assertEqual(policy['modes']['dine_in']['required_fields'], ['name', 'phone', 'table_id'])

    def test_reconnect_preserves_database_and_identity(self):
        lease, identity, s = self.provision('s131_checkout_reconnect')
        b = self.provisioner.binding(lease)
        b['chat'].state = {'checkout': {'quote': {'total': '920'}, 'basket': {'items': []}}}
        b['chat'].save()
        session = SessionStore(session_key=b['browser_session'])
        session[b['namespace']] = {'customer_id': str(b['customer'].pk), 'basket': {'items': ['stale']}}
        session.save()
        self.controls.before_turn(lease, identity, s, 12)
        b['chat'].refresh_from_db()
        self.assertEqual(b['chat'].state['checkout']['quote']['total'], '920')
        session = SessionStore(session_key=b['browser_session'])
        self.assertEqual(session[b['namespace']], {'customer_id': str(b['customer'].pk)})

    def test_terminal_history_uses_authenticated_provider_capture(self):
        lease, _, _ = self.provision('s136_new_order_after_terminal')
        b = self.provisioner.binding(lease)
        b['chat'].refresh_from_db()
        self.assertEqual(b['chat'].order.payment_status, 'paid')
        self.assertEqual(b['chat'].order.order_status, 'delivered')
        self.assertTrue(cm.Inbox.objects.filter(connection__location__tenant=b['tenant'], status='processed', event_type='payment.updated').exists())
        worker = self.runtime.workers[lease.handle]['payment']
        self.assertEqual(worker.db.execute('SELECT COUNT(*) FROM provider_inbox').fetchone()[0], 1)
        self.runtime.pump(lease)
        b['chat'].order.refresh_from_db()
        self.assertEqual(b['chat'].order.order_status, 'delivered')
        self.assertEqual(b['chat'].order.commerce_record.pos_state, 'delivered')

    def test_every_unblocked_case_provisions_and_cleans_without_model_calls(self):
        from evaluate.scenarios.plan import readiness
        for scenario in self.bundle.scenarios:
            if readiness(scenario):
                continue
            with self.subTest(scenario=scenario.scenario_id):
                lease, _, _ = self.provision(scenario.source_id)
                manifest = self.provisioner.inspect(lease)
                self.assertEqual(manifest['scenario_id'], scenario.scenario_id)
                capabilities = manifest['publication']['capabilities']
                self.assertTrue(set(map(tuple, capabilities['required_routes'])) <=
                                set(map(tuple, capabilities['enabled_routes'])))
                self.provisioner.cleanup(lease, force=True)
                self.assertFalse(TenantInfo.objects.filter(pk__in=manifest['owned_tenant_ids']).exists())

    def test_ambiguous_address_selection_seeds_distinct_labels(self):
        from evaluate.scenarios.plan import readiness
        scenario = self.cases['s115_address_ambiguous_selection']
        self.assertEqual(readiness(scenario), [])
        lease, _, _ = self.provision(scenario.source_id)
        labels = set(self.provisioner.binding(lease)['customer'].addresses.values_list('label', flat=True))
        self.assertEqual(labels, {'Home', 'Work'})

    def test_duplicate_address_labels_stay_blocked_without_mutations(self):
        from evaluate.fixtures.definitions import FIXTURES, Fixture, fixture_hashes
        from evaluate.scenarios.plan import readiness
        from evaluate.tests.fakes import make_action, make_scenario
        FIXTURES['dup-labels'] = Fixture(kind='addresses', addresses=['home64', 'home57'])
        self.addCleanup(FIXTURES.pop, 'dup-labels', None)
        action = make_action('seed', {
            'kind': 'seed_fixture', 'fixture_id': 'dup-labels',
            'fixture_hash': fixture_hashes()['dup-labels'],
        })
        scenario = make_scenario('dup-labels', ['Use Home or Work.'], actions=[action])
        self.assertEqual(readiness(scenario)[0]['code'], 'duplicate_address_label')
        count = TenantInfo.objects.count()
        with self.assertRaisesMessage(Blocked, 'duplicate saved-address labels'):
            self.runtime.attest(scenario)
        self.assertEqual(TenantInfo.objects.count(), count)

    def pending_order(self, lease, *, fee='100'):
        from evaluate.fixtures.seeds import basket
        from commerce.pricing import calculate
        from commerce.services import accept_order
        b = self.provisioner.binding(lease)
        _, owner = self.provisioner.owned(lease)
        selected = basket(owner, {'Pistachio Ice Cream': 2})
        config = cm.Configuration.objects.get(tenant=b['tenant'])
        pricing = calculate(selected['items'], config.policy, mode='delivery', fee=fee)
        pricing['location_id'] = str(config.location_id)
        order = om.Order.objects.create(tenant=b['tenant'], customer=b['customer'], source='website',
            payment_mode='online', total_amount=pricing['total_minor'] / 100,
            meta={'checkout': {'mode': 'delivery', 'fields': {'name': 'QA Guest', 'phone': '0000000000',
                                                           'address': 'Test address', 'postal_code': '122102'}}})
        record = accept_order(order, pricing)
        b['chat'].order = order
        b['chat'].save()
        return order, record.payments.get()

    def test_payment_boundary_matches_amount_and_does_not_deliver(self):
        from unittest.mock import patch
        from django.db import connection
        lease, identity, s = self.provision('s122_delivery_online_end_to_end')
        order, payment = self.pending_order(lease)
        baseline_depth = len(connection.atomic_blocks)
        original_payment = self.runtime.payment

        def remote_payment(*args, **kwargs):
            self.assertEqual(len(connection.atomic_blocks), baseline_depth,
                             'Provider I/O must not hold the action transaction open')
            original_payment(*args, **kwargs)
            tenant, owner = self.provisioner.owned(lease)
            owner['callback_marker'] = 'preserve concurrent callback metadata'
            self.provisioner.save_owner(tenant, owner)

        with patch.object(self.runtime, 'payment', side_effect=remote_payment):
            event, = self.controls.before_turn(lease, identity, s, 20)
        tenant, owner = self.provisioner.owned(lease)
        self.assertEqual(owner['callback_marker'], 'preserve concurrent callback metadata')
        self.assertEqual(event.original_turn_index, 20)
        order.refresh_from_db()
        payment.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
        self.assertNotEqual(order.order_status, 'delivered')
        self.assertEqual(payment.captured_minor, 102000)
        self.assertEqual(self.controls.before_turn(lease, identity, s, 20)[0].event_id, event.event_id)
        self.provisioner.cleanup(lease, force=True)

    def test_wrong_capture_amount_does_not_emit_provider_event(self):
        lease, identity, s = self.provision('s122_delivery_online_end_to_end')
        order, payment = self.pending_order(lease, fee='0')
        with self.assertRaisesMessage(Blocked, 'amount'):
            self.controls.before_turn(lease, identity, s, 20)
        self.assertFalse(cm.Inbox.objects.exists())
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'unpaid')

    def test_lost_creation_response_reconciles_after_durable_reattach(self):
        lease, identity, s = self.provision('s132_online_provider_unavailable')
        self.controls.before_turn(lease, identity, s, 12)
        order, payment = self.pending_order(lease)
        self.runtime.pump(lease)
        worker = self.runtime.workers[lease.handle]['payment']
        self.assertEqual(worker.provider.provider.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0], 1)
        self.assertTrue(cm.Command.objects.filter(connection=payment.connection, kind='payment.create', status='unknown').exists())
        self.runtime.release(lease)
        self.runtime.attach(lease)
        self.assertEqual(self.runtime.workers[lease.handle]['payment'].provider.creation_fault, 'after_commit')
        self.controls.before_turn(lease, identity, s, 14)
        self.assertTrue(cm.Command.objects.filter(connection=payment.connection, kind='payment.reconcile').exists())
        payment.refresh_from_db()
        self.assertTrue(payment.external_id)
        self.assertTrue(payment.checkout_url)
        self.assertEqual(cm.Payment.objects.filter(accepted_order=payment.accepted_order).count(), 1)
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'unpaid')
        self.assertFalse(cm.Payment.objects.exclude(captured_minor=0).exists())

    def test_remaining_catalog_fee_and_coverage_actions(self):
        for source, boundary in [('s130_checkout_item_unavailable', 12), ('s143_quote_config_changed', 16)]:
            lease, identity, s = self.provision(source)
            b = self.provisioner.binding(lease)
            from evaluate.fixtures.seeds import basket
            _, owner = self.provisioner.owned(lease)
            b['chat'].state = {'checkout': {'quote': {'total': '1020'}, 'mode': 'delivery',
                                           'basket': basket(owner, {'Pistachio Ice Cream': 2})}}
            b['chat'].save()
            event, = self.controls.before_turn(lease, identity, s, boundary)
            self.assertEqual(event.status, 'succeeded')
            if source.startswith('s130'):
                self.assertFalse(om.MenuItemVariant.objects.get(menu_item__tenant=b['tenant'], menu_item__name='Pistachio Ice Cream').is_available)
            else:
                self.assertEqual(om.CheckoutSettings.objects.get(tenant=b['tenant']).configuration['modes']['delivery']['fee'], '175')
        lease, identity, s = self.provision('s117_coverage_failure_retry')
        self.controls.before_turn(lease, identity, s, 0)
        self.assertEqual(self.provisioner.owned(lease)[1]['lookup']['coverage'], 'unavailable')
        self.controls.before_turn(lease, identity, s, 4)
        self.assertEqual(self.provisioner.owned(lease)[1]['lookup']['coverage'], 'success')

    def test_runtime_context_faults_clock_isolation_and_restore(self):
        from importlib import import_module
        from unittest.mock import patch
        from chatbot_core.logic.cafe import checkout
        from chatbot_core.knowledge_cache import get_item_pricing_cache
        from django.core.cache import cache
        from chatbot_core.llm.schemas import NormalizedClassifiedMessages
        from langchain_core.messages import AIMessage
        from evaluate.controls.cache import cache as response_cache
        original_clock = checkout.timezone
        lease, identity, s = self.provision('s147_classifier_fault_recovery')
        other, _, _ = self.provision('s01_add_pistachio')
        self.controls.before_turn(lease, identity, s, 0)
        cache.set('unrelated-evaluation-sentinel', 'retain')
        classifier = import_module('chatbot_core.logic.cafe.prompts.normalize_and_classify')
        original_chain = classifier.structured_chain
        original_lookup = classifier._cached_proposal
        tenant_key = str(self.provisioner.binding(lease)['tenant'].pk)
        proposal = NormalizedClassifiedMessages(classifications=[{
            'query': 'Add Pistachio', 'intent': 'placing_order', 'sub_intent': 'add_to_basket',
            'rephrased_sentence': 'Add Pistachio',
            'reply_to': None, 'clarification': None,
        }], declared_constraints=[])
        expected = proposal
        with patch('httpx.Client.send', side_effect=AssertionError('No paid API calls permitted')):
            # Warm the real exact cache: the injected timeout must beat this hit.
            with patch.object(classifier, 'structured_chain') as chain:
                chain.return_value.invoke.return_value = {
                    'raw': AIMessage(content='', response_metadata={'finish_reason': 'stop'}),
                    'parsed': proposal, 'parsing_error': None,
                }
                self.assertEqual(classifier.normalize_and_classify('Add Pistachio', tenant_key=tenant_key), expected)
                chain.return_value.invoke.assert_called_once()
            with self.runtime.turn(lease):
                self.assertEqual(checkout.timezone.now().isoformat(), '2026-09-29T14:00:00+05:30')
                with self.assertRaises(Blocked):
                    with self.runtime.turn(other):
                        pass
                self.assertEqual(response_cache.get('unrelated-evaluation-sentinel'), 'retain')
                with self.assertLogs(classifier.logger, level='ERROR'), \
                        self.assertRaises(classifier.NormalizationClassificationError):
                    classifier.normalize_and_classify('Add Pistachio', tenant_key=tenant_key)
                menu = get_item_pricing_cache().get(self.provisioner.binding(lease)['tenant'].api_key)
                self.assertIsNone(menu['Pistachio Ice Cream']['available_quantity'])
        self.assertIs(checkout.timezone, original_clock)
        self.assertIs(classifier.structured_chain, original_chain)
        self.assertIs(classifier._cached_proposal, original_lookup)
        self.assertEqual(cache.get('unrelated-evaluation-sentinel'), 'retain')
        self.controls.before_turn(lease, identity, s, 2)
        with self.runtime.turn(lease):
            self.assertIs(classifier.structured_chain, original_chain)
            self.assertIs(classifier._cached_proposal, original_lookup)
            with patch.object(classifier, 'structured_chain', side_effect=AssertionError('Expected warm cache')):
                self.assertEqual(classifier.normalize_and_classify('Add Pistachio', tenant_key=tenant_key), expected)

    def test_runtime_coverage_is_tenant_scoped_and_restored(self):
        from chatbot_core.logic.cafe.intent_handler import location_based
        original = location_based.verify_delivery_pincode
        lease, _, _ = self.provision('s06_save_address')
        b = self.provisioner.binding(lease)
        with self.runtime.turn(lease):
            self.assertTrue(location_based.verify_delivery_pincode(b['tenant'], '122011'))
            foreign = TenantInfo.objects.create(slug='foreign-coverage', display_name='Other')
            with self.assertRaisesMessage(Blocked, 'another tenant'):
                location_based.verify_delivery_pincode(foreign, '122011')
        self.assertIs(location_based.verify_delivery_pincode, original)

    def test_cross_customer_fixture_does_not_copy_ownership(self):
        lease, _, _ = self.provision('s137_cross_customer_recovery')
        b = self.provisioner.binding(lease)
        _, owner = self.provisioner.owned(lease)
        stranger = om.ChatSession.objects.get(pk=owner['maps']['session:canary'])
        self.assertNotEqual(stranger.customer_id, b['customer'].pk)
        self.assertEqual(stranger.tenant_id, b['tenant'].pk)
        self.assertTrue(stranger.state['checkout']['fields']['name'].startswith('CANARY-'))
        self.assertEqual(b['chat'].state, {})
        self.assertEqual(SessionStore(session_key=b['browser_session'])[b['namespace']],
                         {'customer_id': str(b['customer'].pk), 'basket': {'items': []}})

    def test_tampered_action_and_failed_writer_never_replay_mutations(self):
        from unittest.mock import Mock
        lease, identity, s = self.provision('s144_closing_revalidation')
        action = s.actions[0].model_copy(update={'original_turn_index': 0})
        with self.assertRaisesMessage(Blocked, 'exact reviewed'):
            self.controls.apply(lease, identity, action)
        writer = Mock()
        writer.flush.side_effect = OSError('Disk full')
        controls = DatasetControls(self.provisioner, self.plan, writer)
        with self.assertRaises(OSError):
            controls.apply(lease, identity, s.actions[0])
        writer.flush.side_effect = None
        first = writer.write.call_args.args[0]
        second = controls.apply(lease, identity, s.actions[0])
        self.assertEqual(first.event_id, second.event_id)

    def test_cli_provision_inspect_cleanup_no_chat(self):
        import json
        from contextlib import redirect_stdout
        from io import StringIO
        from evaluate.fixtures.__main__ import main
        folder = Path(self.temp.name)
        config = folder / 'config.json'
        config.write_text(self.config.model_dump_json())
        manifest = folder / 'manifest.json'
        state = folder / 'providers'
        with redirect_stdout(StringIO()):
            self.assertEqual(main(['provision', '--config', str(config), '--scenario', 'qa:stock',
                                   '--state-dir', str(state), '--output', str(manifest)]), 0)
            self.assertEqual(main(['inspect', '--manifest', str(manifest), '--state-dir', str(state)]), 0)
            self.assertEqual(main(['cleanup', '--manifest', str(manifest), '--state-dir', str(state)]), 0)
            self.assertEqual(main(['cleanup', '--manifest', str(manifest), '--state-dir', str(state)]), 0)
        data = json.loads(manifest.read_text())
        self.assertFalse(TenantInfo.objects.filter(pk=data['identities']['tenant']).exists())
        self.assertEqual(list(state.iterdir()), [])

    def test_cleanup_refuses_unrelated_files_and_browser_namespace(self):
        from uuid import uuid4
        lease, _, _ = self.provision('s122_delivery_online_end_to_end')
        b = self.provisioner.binding(lease)
        foreign = Path(self.temp.name) / lease.handle / (str(uuid4()) + '-provider.sqlite3')
        foreign.write_text('unrelated')
        with self.assertRaisesMessage(Blocked, 'Unknown provider directory'):
            self.provisioner.cleanup(lease, force=True)
        self.assertTrue(TenantInfo.objects.filter(pk=b['tenant'].pk).exists())
        self.assertEqual(foreign.read_text(), 'unrelated')
        foreign.unlink()
        browser = SessionStore(session_key=b['browser_session'])
        browser['unrelated'] = 'retain'
        browser.save()
        with self.assertRaisesMessage(Blocked, 'unrelated data'):
            self.provisioner.cleanup(lease, force=True)
        self.assertTrue(TenantInfo.objects.filter(pk=b['tenant'].pk).exists())
        self.assertEqual(SessionStore(session_key=b['browser_session'])['unrelated'], 'retain')

    def test_base_provision_failure_rolls_back_everything(self):
        from unittest.mock import patch
        count = TenantInfo.objects.count()
        with patch.object(self.runtime, 'prepare', side_effect=Blocked('Lane cannot start')):
            with self.assertRaises(Blocked):
                self.provision('s122_delivery_online_end_to_end')
        self.assertEqual(TenantInfo.objects.count(), count)
        self.assertFalse(cm.Connection.objects.exists())
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_synthetic_website_customer_binding_is_real_and_private(self):
        from django.test import RequestFactory
        from chatbot_core.channels.website import _website_customer
        from chatbot_core.logic.cafe.session.django import DjangoSessionStore
        lease, _, _ = self.provision('s26_two_addresses_then_choose')
        b = self.provisioner.binding(lease)
        request = RequestFactory().post('/agent_core/chatbot-api/')
        request.session = SessionStore(session_key=b['browser_session'])
        store = DjangoSessionStore(request, tenant_id=b['tenant'].pk)
        actual = _website_customer(request, b['tenant'], store)
        self.assertEqual(actual.pk, b['customer'].pk)
        self.assertEqual(actual.addresses.count(), 2)

    def test_failed_typed_action_preserves_and_blocks_ambiguous_retry(self):
        from unittest.mock import patch
        lease, identity, s = self.provision('s144_closing_revalidation')
        with patch.object(self.controls, 'mutate', side_effect=OSError('Control transport interrupted')):
            with self.assertRaises(OSError):
                self.controls.before_turn(lease, identity, s, 12)
        report = self.provisioner.inspect(lease)
        self.assertEqual(report['lifecycle'], 'failed_preserved')
        entry = report['actions'][s.actions[0].action_id]
        self.assertEqual(entry['event']['status'], 'failed')
        self.assertIn('before', entry)
        with self.assertRaisesMessage(Blocked, 'incomplete or failed'):
            self.controls.before_turn(lease, identity, s, 12)

    def test_false_zero_empty_profile_overrides_are_preserved(self):
        from evaluate.contracts.models import Setup
        setup, _ = resolve_profiles(['checkout_sandbox'], self.plan, Setup(
            payment='unavailable', payment_methods=['cash'], scheduling=False,
            preparation_minutes=0, delivery_fee_minor=0, allowed_postal_codes=[], required_pickup=[]))
        from evaluate.fixtures.provision import checkout_policy
        policy = checkout_policy(setup)
        self.assertEqual(policy['delivery_postal_codes'], [])
        self.assertEqual(policy['modes']['pickup']['required_fields'], [])
        self.assertEqual(policy['modes']['pickup']['preparation_minutes'], 0)
        self.assertFalse(policy['modes']['pickup']['scheduling_enabled'])
        self.assertEqual(policy['modes']['delivery']['fee'], '0')

    def test_unconditional_runner_cleanup_preserves_unknown_outcome(self):
        lease, _, _ = self.provision('s122_delivery_online_end_to_end')
        self.provisioner.cleanup(lease)
        self.assertEqual(self.provisioner.inspect(lease)['lifecycle'], 'preserved_unconfirmed')
        self.assertTrue((Path(self.temp.name) / lease.handle / 'ownership.json').exists())
        self.provisioner.cleanup(lease)
        self.runtime.attach(lease)
        self.provisioner.finish(lease, succeeded=True)
        self.assertFalse((Path(self.temp.name) / lease.handle).exists())
        self.provisioner.cleanup(lease)

    def test_actual_address_handler_saves_without_coordinates(self):
        from evaluate.fixtures.definitions import address
        from chatbot_core.logic.cafe.intent_handler.location_based import LocationBasedIntent
        lease, _, _ = self.provision('s06_save_address')
        b = self.provisioner.binding(lease)
        handler = LocationBasedIntent(main_query='Yes', sub_intent='confirm_delivery_address',
            tenant=b['tenant'].pk, chat_id=b['browser_session'])
        draft = address('flat12')['components']
        with self.runtime.turn(lease):
            handler.add_delivery_address(b['customer'], draft, label='Home')
        saved = om.CustomerAddress.objects.get(tenant=b['tenant'], customer=b['customer'])
        self.assertEqual(saved.components['postal_code'], '122011')
        self.assertIsNone(saved.location_coordinates)
        self.assertEqual(b['customer'].addresses.count(), 1)

    def test_missing_receipts_block_repeated_attach_without_partial_workers(self):
        from unittest.mock import patch
        lease, _, _ = self.provision('s122_delivery_online_end_to_end')
        self.runtime.release(lease)
        paths = sorted((Path(self.temp.name) / lease.handle).glob('*-provider.sqlite3'))
        missing = paths[-1].resolve()
        actual_is_file = Path.is_file
        def is_file(path):
            return False if path.resolve() == missing else actual_is_file(path)
        with patch.object(Path, 'is_file', is_file):
            for _ in range(2):
                with self.assertRaises(Blocked):
                    self.runtime.attach(lease)
                self.assertNotIn(lease.handle, self.runtime.workers)
        with self.assertRaisesMessage(Blocked, 'detached'):
            self.runtime.pump(lease)
        with self.assertRaisesMessage(Blocked, 'detached'):
            with self.runtime.turn(lease):
                self.fail('Detached runtime must not execute user turns')
        self.runtime.attach(lease)
        self.assertEqual(set(self.runtime.workers[lease.handle]), {'payment', 'pos'})

    def test_controls_reject_changed_plan_and_omitted_actions(self):
        lease, identity, scenario = self.provision('s144_closing_revalidation')
        with self.assertRaisesMessage(Blocked, 'actions differ'):
            self.controls.before_turn(lease, identity, scenario.model_copy(update={'actions': []}), 12)
        altered_plan = self.plan.model_copy(deep=True)
        altered_plan.default_clock.at = '2026-09-29T15:00:00+05:30'
        changed_controls = DatasetControls(self.provisioner, altered_plan)
        with self.assertRaisesMessage(Blocked, 'plan used to provision'):
            changed_controls.apply(lease, identity, scenario.actions[0])
        self.assertEqual(self.provisioner.inspect(lease)['actions'], {})

    def test_no_action_boundary_still_validates_lease_identity(self):
        lease, identity, scenario = self.provision('stock')
        wrong = identity.model_copy(update={'attempt': 2})
        with self.assertRaisesMessage(Blocked, 'does not own'):
            self.controls.before_turn(lease, wrong, scenario, 0)

    def test_missing_chat_prerequisite_is_blocked(self):
        lease, identity, scenario = self.provision('s131_checkout_reconnect')
        binding = self.provisioner.binding(lease)
        binding['chat'].is_completed = True
        binding['chat'].save(update_fields=['is_completed'])
        with self.assertRaisesMessage(Blocked, 'active owned chat'):
            self.controls.before_turn(lease, identity, scenario, 12)
