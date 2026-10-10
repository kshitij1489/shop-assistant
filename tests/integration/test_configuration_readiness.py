"""Production configuration mistakes must not hide request meanings or drafts."""
import importlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from collections import OrderedDict
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from chatbot_core import knowledge_cache, runtime_configuration as runtime
from chatbot_core.configuration_imports import import_configuration
from chatbot_core.intent_definitions import STANDARD_INTENTS
from chatbot_core.models import TenantInfo, TenantJSONDoc
from orders.models import Customer, ChatSession
from tests.support.runtime import classification_result
from users.models import TenantProfile


class ConfigurationReadinessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.enterContext(patch.object(runtime, '_cache', OrderedDict()))
        from chatbot_core import knowledge_retrieval
        self.enterContext(patch.object(knowledge_retrieval, '_indexes', OrderedDict()))
        self.tenant = TenantInfo.objects.create(display_name='Readiness cafe', approval_status='APPROVED')
        self.other = TenantInfo.objects.create(display_name='Other cafe', approval_status='APPROVED')
        user = User.objects.create_user(username='readiness-owner')
        TenantProfile.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)
        self.publication = runtime.publish_default_configuration(self.tenant.pk)

    def save(self, dtype, intent, topic, payload, tenant=None):
        return TenantJSONDoc.objects.update_or_create(tenant=tenant or self.tenant,
            dtype=dtype, intent=intent, sub_intent=topic, defaults={'payload': payload})[0]

    def publish(self):
        self.publication = runtime.publish_configuration(self.tenant.pk, expected_version=self.publication.version)

    def test_standard_request_recognition_survives_missing_routes_and_bad_legacy_descriptions(self):
        self.save('intent_classification', 'menu_items', 'explore_options', 'Use only supplied knowledge.')
        self.save('response_intents', 'menu_items', 'explore_options', 'Answer concisely.')
        self.save('knowledge', 'menu_items', 'explore_options', {'items': ['Coffee']})
        self.publish()
        schema = knowledge_cache.get_intent_classification_cache(self.tenant.pk)
        self.assertEqual(schema['menu_items']['explore_options']['description'],
                         STANDARD_INTENTS['menu_items']['explore_options']['description'])
        self.assertIn('location_and_hours', schema['information_about_the_cafe'])
        self.assertIn('initiate_order', schema['placing_order'])
        self.assertFalse(runtime.get_configuration(tenant_id=self.tenant.pk).allows('placing_order', 'initiate_order'))

    def test_screenshot_sequence_handles_missing_hours_knowledge_only_menu_and_disabled_orders(self):
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore
        from tests.support.replies import install_reply_renderer
        runner = importlib.import_module('chatbot_core.logic.cafe.workflow.runner')
        graph = importlib.import_module('chatbot_core.logic.cafe.workflow.graph')
        knowledge = importlib.import_module('chatbot_core.logic.cafe.prompts.generate_response_from_knowledge')
        install_reply_renderer(self)
        customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='123')
        ChatSession.objects.create(tenant=self.tenant, customer=customer, platform='website', session_id='readiness-user')
        self.enterContext(patch('chatbot_core.logic.cafe.session.memory._session_data', {}))
        session = MemorySessionStore('readiness-user', tenant_id=self.tenant.pk, platform='website')
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))
        self.enterContext(patch.object(knowledge, 'enqueue_string'))
        chain = Mock(invoke=Mock(return_value='Our menu includes Coffee and Cake.'))
        with patch.object(graph, 'normalize_and_classify') as classifier, \
                patch.object(knowledge, 'text_chain', return_value=chain) as answer:
            classifier.return_value = classification_result([
                ('Hello', 'general', 'greeting', None, None)])
            response, _ = runner.run_conversation(self.tenant, session, 'Hello', customer)
            self.assertIn('Hello!', response)
            classifier.return_value = classification_result([
                ('Are you open right now', 'information_about_the_cafe', 'location_and_hours', None, None)])
            response, _ = runner.run_conversation(self.tenant, session, 'Are you open right now', customer)
            self.assertIn("don't have enough information", response)
            self.assertNotIn("can't help with that request", response)
            chain.invoke.assert_not_called()
            self.save('knowledge', 'menu_items', 'explore_options', {'items': ['Coffee', 'Cake']})
            self.publish()
            classifier.return_value = classification_result([
                ("What's in menu", 'menu_items', 'explore_options', None, None)])
            response, _ = runner.run_conversation(self.tenant, session, "What's in menu", customer)
            self.assertEqual(response, 'Our menu includes Coffee and Cake.')
            self.assertIn('Coffee', answer.call_args.args[0])
            classifier.return_value = classification_result([
                ('Can I order', 'placing_order', 'initiate_order', None, None)])
            response, basket = runner.run_conversation(self.tenant, session, 'Can I order', customer)
            self.assertIn('unavailable', response)
            self.assertEqual(basket, [])
            self.assertEqual(chain.invoke.call_count, 1)

    def test_standard_knowledge_defaults_preserve_draft_disable_and_tenant_boundaries(self):
        from chatbot_core import knowledge_retrieval
        self.save('knowledge', 'information_about_the_cafe', 'location_and_hours', {'hours': 'Daily 09:00-18:00'})
        self.save('knowledge', 'menu_items', 'menu_category', {'Coffee': 'Drinks'})
        self.save('knowledge', 'information_about_the_cafe', 'private_topic', 'OTHER TENANT FACTS', self.other)
        self.publish()
        self.save('knowledge', 'information_about_the_cafe', 'location_and_hours', {'hours': 'UNPUBLISHED HOURS'})
        with patch.object(knowledge_retrieval, '_live_menu', return_value=None), \
                patch.object(knowledge_retrieval, 'inventory_knowledge', return_value={'records': []}):
            result = knowledge_retrieval.retrieve_knowledge(self.tenant.api_key,
                'information_about_the_cafe', 'location_and_hours', 'Are you open right now')['payload']
            encoded = json.dumps(result)
            self.assertIn('Daily 09:00-18:00', encoded)
            self.assertNotIn('UNPUBLISHED', encoded)
            self.assertNotIn('OTHER TENANT', encoded)
            configuration = runtime.get_configuration(tenant_id=self.tenant.pk)
            self.assertFalse(configuration.allows('menu_items', 'menu_category'))
            self.save('intent_classification', 'information_about_the_cafe', 'location_and_hours', {'enabled': False})
            self.publish()
            self.assertIsNone(knowledge_retrieval.retrieve_knowledge(self.tenant.api_key,
                'information_about_the_cafe', 'location_and_hours', 'Are you open right now'))
            result = knowledge_retrieval.retrieve_knowledge(self.tenant.api_key, 'menu_items', 'explore_options', 'hours')
            self.assertNotIn('UNPUBLISHED HOURS', json.dumps(result))

    def test_bad_classification_publication_preserves_live_version(self):
        for dtype in ('intent_classification', 'response_intents'):
            self.save(dtype, 'information_about_the_cafe', 'location_and_hours', 'Use only supplied knowledge.')
        self.save('knowledge', 'information_about_the_cafe', 'location_and_hours', {'hours': '09:00-18:00'})
        with self.assertRaisesMessage(ValidationError, 'duplicates the response instructions'):
            self.publish()
        self.publication.refresh_from_db()
        self.assertEqual(self.publication.version, 1)
        self.assertFalse(any(d['intent'] == 'information_about_the_cafe' for d in self.publication.documents))

    def test_bulk_wrong_type_and_raw_editor_reject_instruction_strings_as_classifications(self):
        source = {'menu_items': {'explore_options': 'Use supplied knowledge.'}}
        with self.assertRaisesMessage(ValueError, 'must be an object'):
            import_configuration(self.tenant, 'intent_classification', {
                'document_type': 'intent_classification', 'documents': source})
        response = self.client.post(reverse('tenant:tenant_knowledge'), {
            'action': 'update', 'dtype': 'intent_classification', 'intent': 'menu_items',
            'sub_intent': 'explore_options', 'payload': json.dumps('Use supplied knowledge.'),
        }, follow=True)
        self.assertContains(response, 'must be an object')
        self.assertFalse(TenantJSONDoc.objects.filter(tenant=self.tenant, dtype='intent_classification', intent='menu_items').exists())

    def test_typed_import_checks_selected_type_before_saving(self):
        source = {'document_type': 'knowledge', 'documents': {
            'information_about_the_cafe': {'location_and_hours': {'hours': '09:00-18:00'}}}}
        for kind in ('response_intents', 'intent_classification'):
            with self.subTest(kind=kind), self.assertRaisesMessage(ValueError, 'document_type'):
                import_configuration(self.tenant, kind, source)
        self.assertFalse(TenantJSONDoc.objects.filter(tenant=self.tenant, intent='information_about_the_cafe').exists())
        import_configuration(self.tenant, 'knowledge', source)
        self.assertEqual(TenantJSONDoc.objects.get(tenant=self.tenant, intent='information_about_the_cafe').payload,
                         {'hours': '09:00-18:00'})

    def test_demo_import_types_cannot_overwrite_published_or_draft_facts(self):
        root = Path(__file__).resolve().parents[2] / 'demo'
        sources = {kind: (root / filename).read_text() for kind, filename in (
            ('knowledge', 'knowledge_base.json'), ('intent_classification', 'intent_classification.json'),
            ('response_intents', 'response_instructions.json'))}
        import_configuration(self.tenant, 'knowledge', sources['knowledge'])
        self.publish()
        saved = list(TenantJSONDoc.objects.filter(tenant=self.tenant).order_by('pk').values())
        live = deepcopy(self.publication.documents)
        for actual, source in sources.items():
            for selected in sources:
                if selected == actual:
                    continue
                with self.subTest(actual=actual, selected=selected):
                    response = self.client.post(reverse('tenant:upload_knowledge_prompt'), {
                        'dtype': selected, 'json_blob': source}, follow=True)
                    self.assertContains(response, 'document_type must match')
        for source in sources.values():
            unwrapped = json.loads(source)['documents']
            with self.assertRaisesMessage(ValueError, 'require document_type'):
                import_configuration(self.tenant, 'knowledge', unwrapped)
        self.assertEqual(saved, list(TenantJSONDoc.objects.filter(tenant=self.tenant).order_by('pk').values()))
        self.publication.refresh_from_db()
        self.assertEqual(live, self.publication.documents)
        # Correctly selected typed files still import and publish together.
        for kind, source in sources.items():
            import_configuration(self.tenant, kind, source)
        from tests.support.ordering import seed_evaluation_policy
        from orders.models import CheckoutSettings
        seed_evaluation_policy(self.tenant)
        CheckoutSettings.objects.create(tenant=self.tenant)
        self.publish()

    def test_open_now_recomputes_schedule_and_cache_at_closing(self):
        from chatbot_core.opening_hours import DAYS
        self.save('knowledge', 'information_about_the_cafe', 'location_and_hours', {
            'timezone': 'Asia/Kolkata', 'opening_hours': {
                'weekly': {day: [{'opens': '12:00', 'closes': '23:30'}] for day in DAYS}}})
        self.publish()
        answer = importlib.import_module('chatbot_core.logic.cafe.prompts.generate_response_from_knowledge')
        chain = Mock(invoke=Mock(side_effect=['Scheduled open.', 'Scheduled closed.']))
        with patch.object(answer, 'text_chain', return_value=chain) as provider, patch.object(answer, 'enqueue_string'):
            responses = []
            for when in ('2026-10-09T17:59:00+00:00', '2026-10-09T17:59:30+00:00', '2026-10-09T18:00:00+00:00'):
                with patch('chatbot_core.opening_hours.timezone.now', return_value=datetime.fromisoformat(when)):
                    responses.append(answer.generate_response_from_knowledge(self.tenant.api_key,
                        'location_and_hours', 'Are you open right now?', main_intent='information_about_the_cafe',
                        response_profile='cafe_information'))
            self.assertEqual(responses, ['Scheduled open.', 'Scheduled open.', 'Scheduled closed.'])
            self.assertEqual(chain.invoke.call_count, 2)
            self.assertIn('"scheduled_status":"open"', provider.call_args_list[0].args[0])
            self.assertIn('"scheduled_status":"closed"', provider.call_args_list[1].args[0])
            self.assertIn('2026-10-09T23:30:00+05:30', provider.call_args_list[1].args[0])

    def test_ordering_information_without_action_permissions_or_limits(self):
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore
        from chatbot_core.llm.schemas import ActionProposal
        from tests.support.replies import install_reply_renderer
        from orders.models import Order
        runner = importlib.import_module('chatbot_core.logic.cafe.workflow.runner')
        graph = importlib.import_module('chatbot_core.logic.cafe.workflow.graph')
        answer = importlib.import_module('chatbot_core.logic.cafe.prompts.generate_response_from_knowledge')
        install_reply_renderer(self)
        customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='123')
        ChatSession.objects.create(tenant=self.tenant, customer=customer, platform='website', session_id='info-user')
        self.enterContext(patch('chatbot_core.logic.cafe.session.memory._session_data', {}))
        session = MemorySessionStore('info-user', tenant_id=self.tenant.pk, platform='website')
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))
        self.enterContext(patch.object(answer, 'enqueue_string'))
        for topic in ('how_to_order', 'order_channels_and_modes'):
            self.save('knowledge', 'placing_order', topic, {'instructions': 'Order by telephone; pickup is available.'})
        self.publish()
        config = runtime.get_configuration(tenant_id=self.tenant.pk)
        self.assertTrue(config.allows_information('placing_order', 'order_channels_and_modes'))
        self.assertFalse(config.allows('placing_order', 'order_channels_and_modes'))
        chain = Mock(invoke=Mock(return_value='You can order by telephone for pickup.'))
        with patch.object(graph, 'normalize_and_classify') as classifier, patch.object(answer, 'text_chain', return_value=chain):
            for topic, query in [('how_to_order', 'How do I order by phone?'),
                                 ('order_channels_and_modes', 'Do you offer pickup?')]:
                classifier.return_value = classification_result([(query, 'placing_order', topic, None, None, None)])
                response, basket = runner.run_conversation(self.tenant, session, query, customer)
                self.assertIn('telephone', response)
                self.assertEqual(basket, [])
            classifier.return_value = classification_result([('pickup please', 'placing_order',
                'order_channels_and_modes', None, None, ActionProposal(kind='SET_FULFILLMENT', value='pickup'))])
            response, basket = runner.run_conversation(self.tenant, session, 'pickup please', customer)
            self.assertIn('unavailable', response)
            self.assertNotIn('fulfillment_preference', session.get_checklist())
            self.assertFalse(Order.objects.filter(tenant=self.tenant).exists())
        # Explicitly configuring just how-to-order needs no transactional settings.
        self.save('intent_classification', 'placing_order', 'how_to_order', {'description': 'Questions about how to order.'})
        self.save('response_intents', 'placing_order', 'how_to_order', 'Use the published ordering instructions.')
        self.publish()
        self.save('intent_classification', 'placing_order', 'order_channels_and_modes', {'enabled': False})
        self.publish()
        self.assertFalse(runtime.get_configuration(tenant_id=self.tenant.pk).allows_information(
            'placing_order', 'order_channels_and_modes'))

    def test_dashboard_compares_live_values_with_changed_and_deleted_drafts(self):
        self.save('knowledge', 'menu_items', 'explore_options', {'items': ['Coffee']})
        self.publish()
        self.save('knowledge', 'menu_items', 'explore_options', {'items': ['Unpublished cake']})
        self.save('knowledge', 'information_about_the_cafe', 'location_and_hours', {'hours': '09:00-18:00'})
        TenantJSONDoc.objects.filter(tenant=self.tenant, dtype='response_intents', sub_intent='thanks').delete()
        response = self.client.get(reverse('tenant:tenant_knowledge'))
        report = response.context['configuration_report']
        self.assertEqual(report['changed_count'], 3)
        rows = {(row['intent'], row['topic']): row for row in report['rows']}
        hours = rows['information_about_the_cafe', 'location_and_hours']
        self.assertEqual(hours['live'], 'No facts saved for this topic')
        self.assertEqual(hours['draft'], 'Facts available with application defaults')
        menu = rows['menu_items', 'explore_options']
        self.assertIn('Coffee', menu['published_values'])
        self.assertNotIn('Unpublished cake', menu['published_values'])
        self.assertContains(response, '3 saved documents with unpublished changes')
        self.assertContains(response, 'Ordering')
        self.assertContains(response, 'View published values')

    def test_publication_reports_limitations_and_draft_validation_problems(self):
        response = self.client.post(reverse('tenant:tenant_knowledge'), {
            'action': 'publish', 'version': 1}, follow=True)
        self.assertContains(response, 'Published with limitations:')
        self.assertContains(response, 'Ordering is disabled.')
        self.save('intent_classification', 'information_about_the_cafe', 'parking', {
            'description': 'Questions about parking.'})
        self.save('response_intents', 'information_about_the_cafe', 'parking', 'Use the published parking facts.')
        response = self.client.get(reverse('tenant:tenant_knowledge'))
        self.assertContains(response, 'Fix these draft problems before publishing')
        self.assertContains(response, 'required knowledge')

    def test_custom_faq_recognition_is_published_and_tenant_scoped(self):
        for tenant, topic in ((self.tenant, 'pet_policy'), (self.other, 'other_private_topic')):
            self.save('intent_classification', 'information_about_the_cafe', topic,
                      {'description': 'Questions about pets.', 'enabled': False}, tenant)
        self.publish()
        schema = knowledge_cache.get_intent_classification_cache(self.tenant.pk)
        self.assertIn('pet_policy', schema['information_about_the_cafe'])
        self.assertNotIn('other_private_topic', schema['information_about_the_cafe'])
        self.assertFalse(runtime.get_configuration(tenant_id=self.tenant.pk).allows('information_about_the_cafe', 'pet_policy'))

    def test_dashboard_uses_external_menu_freshness_instead_of_uploaded_facts(self):
        from commerce.models import Location, Connection
        from commerce.menu_sync import configure_source, import_snapshot
        from django.utils import timezone
        location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        connection = Connection.objects.create(location=location, provider='json_menu', role='pos',
            account_id='readiness-menu', active=True, capabilities=['catalog.write'], secret_ref='managed:test')
        source = configure_source(self.tenant.pk, mode='external', connection=connection)
        self.save('knowledge', 'menu_items', 'explore_options', {'items': ['OUTDATED UPLOAD']})
        self.publish()
        url = reverse('tenant:tenant_knowledge')
        response = self.client.get(url)
        menu = response.context['configuration_report']['summaries'][0]
        self.assertEqual(menu['state'], 'Synchronized catalog unavailable or stale')
        import_snapshot(connection, {
            'schema_version': 1, 'complete': True, 'source_generation': str(source.generation),
            'sequence': 1, 'revision': 'r1', 'observed_at': timezone.now().isoformat(), 'currency': 'INR',
            'categories': [], 'modifier_groups': [], 'items': [{
                'external_id': 'coffee', 'name': 'Coffee', 'available': True,
                'variants': [{'external_id': 'regular', 'name': 'Regular', 'available': True, 'price': '100'}],
                'modifier_groups': [],
            }],
        })
        response = self.client.get(url)
        self.assertEqual(response.context['configuration_report']['summaries'][0]['state'],
                         'Synchronized catalog available')
