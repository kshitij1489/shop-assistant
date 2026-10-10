"""Publication and worker-refresh contracts with a real DB and mocked providers."""
from tests.support.runtime import classification_result
from collections import OrderedDict
import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from chatbot_core import knowledge_cache, runtime_configuration as runtime
from chatbot_core.models import TenantInfo, TenantJSONDoc, TenantRuntimeConfiguration
from chatbot_core.intent_definitions import STANDARD_INTENTS
from orders.models import MenuItem, MenuItemVariant, CheckoutSettings, Customer, ChatSession
from users.models import TenantProfile


class RuntimeConfigurationTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Patch the provider boundary without removing newly imported native
        # modules (notably NumPy) from sys.modules during class cleanup.
        cls.enterClassContext(patch('chatbot_core.vector_store.embedding_client.get_embedding',
                                   side_effect=AssertionError('Unexpected embedding I/O')))
        cls.runner = importlib.import_module('chatbot_core.logic.cafe.workflow.runner')
        cls.graph = importlib.import_module('chatbot_core.logic.cafe.workflow.graph')
        cls.knowledge = importlib.import_module('chatbot_core.logic.cafe.prompts.generate_response_from_knowledge')

    def setUp(self):
        cache.clear()
        from tests.support.replies import install_reply_renderer
        install_reply_renderer(self)
        self.enterContext(patch.object(runtime, '_cache', OrderedDict()))
        from chatbot_core import knowledge_retrieval
        self.enterContext(patch.object(knowledge_retrieval, '_indexes', OrderedDict()))
        self.tenant = TenantInfo.objects.create(display_name='Runtime cafe', approval_status='APPROVED')
        self.other = TenantInfo.objects.create(display_name='Other cafe', approval_status='APPROVED')
        self.user = User.objects.create_user(username='runtime-owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.topic()

    def topic(self, tenant=None, intent='information_about_the_cafe', sub='pet_policy', knowledge='Pets are welcome outside.', **options):
        tenant = tenant or self.tenant
        data = {'description': 'Questions about pets.', 'enabled': True, 'examples': ['Can I bring my dog?'], **options}
        for dtype, payload in [('intent_classification', data), ('response_intents', 'Answer concisely from the supplied facts.'), ('knowledge', knowledge)]:
            TenantJSONDoc.objects.update_or_create(tenant=tenant, dtype=dtype, intent=intent, sub_intent=sub, defaults={'payload': payload})

    def publish(self, tenant=None):
        tenant = tenant or self.tenant
        version = TenantRuntimeConfiguration.objects.filter(tenant=tenant).values_list('version', flat=True).first() or 0
        return runtime.publish_configuration(tenant.pk, expected_version=version)

    def document(self, tenant=None, intent='information_about_the_cafe', sub='pet_policy'):
        return knowledge_cache.get_knowledge_base_cache().get(((tenant or self.tenant).api_key, intent, sub))

    def test_drafts_stay_inactive_until_validated_publication(self):
        self.assertIsNone(self.document())
        self.assertNotIn('pet_policy', knowledge_cache.get_intent_classification_cache(self.tenant.pk)['information_about_the_cafe'])
        publication = self.publish()
        self.assertEqual(publication.version, 1)
        self.assertEqual(self.document()['identity'], (str(self.tenant.pk), 'knowledge', 'information_about_the_cafe', 'pet_policy', 1))
        self.topic(knowledge='Pets are welcome inside too.')
        self.assertEqual(self.document()['payload'], 'Pets are welcome outside.')
        self.publish()
        self.assertEqual(self.document()['payload'], 'Pets are welcome inside too.')

    def test_version_zero_is_unpublished_even_if_it_contains_documents(self):
        documents = list(TenantJSONDoc.objects.filter(tenant=self.tenant).values('dtype', 'intent', 'sub_intent', 'payload'))
        TenantRuntimeConfiguration.objects.create(tenant=self.tenant, version=0, documents=documents)
        configuration = runtime.get_configuration(tenant_id=self.tenant.pk)
        self.assertFalse(configuration.published)
        self.assertFalse(configuration.allows('information_about_the_cafe', 'pet_policy'))
        self.assertIsNone(self.document())
        self.assertNotIn('pet_policy', knowledge_cache.get_intent_classification_cache(self.tenant.pk)['information_about_the_cafe'])

    def test_signup_publishes_validated_starter_capabilities(self):
        response = self.client.post(reverse('signup'), {'username': 'new-owner', 'email': 'new@example.com',
            'password': 'a-good-password', 'password2': 'a-good-password', 'business_name': 'New cafe', 'business_type': 'cafe'})
        self.assertEqual(response.status_code, 302)
        tenant = TenantInfo.objects.get(display_name='New cafe')
        configuration = runtime.get_configuration(tenant_id=tenant.pk)
        self.assertTrue(configuration.published)
        runtime.validate_documents(tenant, configuration.documents)
        self.assertTrue(configuration.allows('general', 'greeting'))
        self.assertFalse(configuration.allows('placing_order', 'add_to_basket'))
        self.assertFalse(configuration.allows('placing_order', 'order_confirmation'))

    def test_unapproved_inactive_and_master_accounts_cannot_publish(self):
        url = reverse('tenant:tenant_knowledge')
        for status in ('PENDING', 'REJECTED'):
            self.tenant.approval_status = status
            self.tenant.save()
            self.assertIn(self.client.post(url, {'action': 'publish', 'version': 0}).status_code, (302, 403))
            self.assertFalse(TenantRuntimeConfiguration.objects.filter(tenant=self.tenant).exists())
        self.tenant.approval_status = 'APPROVED'
        self.tenant.is_active = False
        self.tenant.save()
        self.assertIn(self.client.post(url, {'action': 'publish', 'version': 0}).status_code, (302, 403))
        self.assertFalse(TenantRuntimeConfiguration.objects.filter(tenant=self.tenant).exists())
        self.tenant.is_active = True
        self.tenant.save()
        profile = self.user.tenantprofile
        profile.is_master = True
        profile.save()
        session = self.client.session
        session['impersonated_tenant_id'] = self.tenant.pk
        session.save()
        self.assertEqual(self.client.post(url, {'action': 'publish', 'version': 0}).status_code, 302)
        self.assertFalse(TenantRuntimeConfiguration.objects.filter(tenant=self.tenant).exists())

    def test_publish_rejects_missing_knowledge_instructions_and_unknown_operations_atomically(self):
        original = self.publish().documents
        bad = [
            ('knowledge', 'information_about_the_cafe', 'pet_policy', {}),
            ('response_intents', 'information_about_the_cafe', 'pet_policy', ''),
            ('intent_classification', 'issue_refund', 'refund', {'description': 'Refund'}),
            ('intent_classification', 'placing_order', 'issue_refund', {'description': 'Refund'}),
            ('intent_classification', 'information_about_the_cafe', 'pet_policy', {'description': 'Pets', 'enabled': 'false'}),
            ('intent_classification', 'information_about_the_cafe', 'pet_policy', {'description': 'Pets', 'examples': [3]}),
        ]
        for dtype, intent, sub, payload in bad:
            with self.subTest(dtype=dtype, intent=intent, payload=payload):
                TenantJSONDoc.objects.filter(tenant=self.tenant).delete()
                self.topic()
                TenantJSONDoc.objects.update_or_create(tenant=self.tenant, dtype=dtype, intent=intent, sub_intent=sub, defaults={'payload': payload})
                with self.assertRaises(ValidationError):
                    self.publish()
                live = TenantRuntimeConfiguration.objects.get(tenant=self.tenant)
                self.assertEqual((live.version, live.documents), (1, original))

    def test_required_settings_knowledge_and_catalog_references_are_tenant_scoped(self):
        item = MenuItem.objects.create(tenant=self.other, name='Other item')
        variant = MenuItemVariant.objects.create(menu_item=item, size='Small', price=10)
        for reference in ({'type': 'item', 'id': str(item.pk)}, {'type': 'variant', 'id': str(variant.pk)},
                          {'type': 'item', 'id': 'bad uuid'}, {'type': [], 'id': 'bad'}):
            self.topic(catalog_references=[reference])
            with self.assertRaises(ValidationError):
                self.publish()
        self.topic(required_settings=['support.email'], required_knowledge=['information_about_the_cafe/support'])
        with self.assertRaises(ValidationError) as caught:
            self.publish()
        self.assertIn('required knowledge', str(caught.exception))
        self.assertIn('required tenant setting', str(caught.exception))
        self.tenant.meta = {'support': {'email': 'help@example.com'}}
        self.tenant.save(update_fields=['meta'])
        self.topic(sub='support', knowledge='Contact our staff.')
        self.publish()
        own = MenuItem.objects.create(tenant=self.tenant, name='Our item')
        self.topic(catalog_references=[{'type': 'item', 'id': str(own.pk)}])
        self.publish()

    def test_checkout_capability_requires_valid_settings(self):
        from tests.support.ordering import seed_evaluation_policy
        self.topic(intent='placing_order', sub='order_confirmation')
        with self.assertRaisesMessage(ValidationError, 'Configure checkout'):
            self.publish()
        CheckoutSettings.objects.create(tenant=self.tenant)
        with self.assertRaisesMessage(ValidationError, 'quantity and amount limits'):
            self.publish()
        seed_evaluation_policy(self.tenant)
        self.publish()
        self.assertTrue(runtime.get_configuration(tenant_id=self.tenant.pk).allows('placing_order', 'order_confirmation'))

    def test_stale_publication_version_cannot_overwrite_newer_configuration(self):
        self.publish()
        with self.assertRaisesMessage(ValidationError, 'version changed'):
            runtime.publish_configuration(self.tenant.pk, expected_version=0)
        with self.assertRaises(ValidationError):
            runtime.publish_configuration(self.tenant.pk, expected_version=True)
        self.assertEqual(TenantRuntimeConfiguration.objects.get(tenant=self.tenant).version, 1)

    def test_worker_checks_shared_version_even_without_notifications(self):
        first = self.publish()
        self.document()  # warm this worker
        self.topic(knowledge='New policy.')
        with patch.object(runtime, '_cache', OrderedDict()):
            self.publish()  # a different worker's local cache
        with self.assertNumQueries(2):
            self.assertEqual(self.document()['payload'], 'New policy.')
        with self.assertNumQueries(1):
            self.assertEqual(self.document()['identity'][-1], first.version + 1)

    def test_caches_separate_tenant_type_intent_topic_and_version(self):
        self.topic(intent='general', sub='greeting', knowledge='Social greeting.')
        self.topic(sub='greeting', knowledge='Cafe greeting policy.')
        self.topic(tenant=self.other, knowledge='Other cafe policy.')
        self.publish()
        self.publish(self.other)
        self.assertEqual(self.document(intent='general', sub='greeting')['payload'], 'Social greeting.')
        self.assertEqual(self.document(sub='greeting')['payload'], 'Cafe greeting policy.')
        self.assertEqual(self.document(self.other)['payload'], 'Other cafe policy.')
        instruction = knowledge_cache.get_intent_prompt_cache().get((self.tenant.api_key, 'information_about_the_cafe', 'pet_policy'))
        self.assertNotEqual(instruction['identity'], self.document()['identity'])
        schema_before = knowledge_cache.get_intent_classification_cache(self.tenant.pk)
        self.publish()  # identical content still gets a fresh version
        schema_after = knowledge_cache.get_intent_classification_cache(self.tenant.pk)
        self.assertEqual(schema_before, schema_after)
        # The combined operation includes this publication version in its key,
        # independently of the schema's JSON content.
        self.assertNotEqual(schema_before.version, schema_after.version)
        self.assertEqual(schema_after['information_about_the_cafe']['pet_policy']['examples'], ['Can I bring my dog?'])

    def test_classifier_keeps_standard_meanings_and_only_published_tenant_examples(self):
        from chatbot_core.llm.schemas import NormalizedClassifiedMessages
        from evaluate.datasets.loader import classification_documents
        from tests.support.paths import REPOSITORY_ROOT

        doc = classification_documents(REPOSITORY_ROOT / 'test_data', {('general', 'greeting')})[0]
        self.topic(intent='general', sub='greeting', examples=['Hello cafe!'], **doc['payload'])
        self.topic(intent='general', sub='thanks', enabled=False)
        self.topic(tenant=self.other, intent='general', sub='greeting', description='Other tenant wording')
        self.publish()
        self.publish(self.other)
        classifier = importlib.import_module('chatbot_core.logic.cafe.prompts.normalize_and_classify')
        parsed = NormalizedClassifiedMessages.model_validate({'declared_constraints': [],
            'classifications': [{'query': 'hi', 'rephrased_sentence': 'Hello', 'intent': 'general',
                'sub_intent': 'greeting', 'reply_to': None, 'clarification': None, 'action': None}]})
        with patch.object(classifier, 'structured_chain') as chain:
            chain.return_value.invoke.return_value = {'parsed': parsed, 'parsing_error': None,
                'raw': SimpleNamespace(response_metadata={}, additional_kwargs={})}
            def prompt_schema():
                classifier.normalize_and_classify('hi', tenant_key=str(self.tenant.pk))
                system = chain.call_args.args[1]
                return json.loads(system[len(classifier.SYSTEM_PROMPT):])

            schema = prompt_schema()
            self.assertEqual(schema['general']['greeting']['description'], doc['payload']['description'])
            self.assertEqual(schema['general']['greeting']['examples'], ['Hello cafe!'])
            self.assertIn('thanks', schema['general'])
            self.assertEqual(schema['general']['thanks'], STANDARD_INTENTS['general']['thanks'])
            self.assertNotIn('Other tenant wording', json.dumps(schema))
            self.topic(intent='general', sub='greeting', description='New published greeting', examples=['Good morning cafe!'])
            self.assertEqual(prompt_schema(), schema)  # Draft edit remains invisible.
            self.publish()
            updated = prompt_schema()['general']['greeting']
            self.assertEqual(updated['description'], STANDARD_INTENTS['general']['greeting']['description'])
            self.assertIn('Good morning cafe!', updated['examples'])
            self.assertNotIn('Hello cafe!', updated['examples'])
            self.assertEqual(chain.call_count, 2)  # Publication also invalidates the exact cache.

    def test_turn_uses_consistent_configuration_and_next_turn_refreshes(self):
        self.publish()
        with runtime.configuration_for_turn(self.tenant.pk):
            self.assertEqual(self.document()['identity'][-1], 1)
            self.topic(knowledge='Updated during this turn.')
            self.publish()
            self.assertEqual(self.document()['identity'][-1], 1)
        with runtime.configuration_for_turn(self.tenant.pk):
            self.assertEqual(self.document()['identity'][-1], 2)
            self.assertEqual(self.document()['payload'], 'Updated during this turn.')

    def conversation(self):
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore
        customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='123')
        ChatSession.objects.create(tenant=self.tenant, customer=customer, platform='website', session_id='runtime-user')
        self.enterContext(patch('chatbot_core.logic.cafe.session.memory._session_data', {}))
        session = MemorySessionStore('runtime-user', tenant_id=self.tenant.pk, platform='website')
        self.enterContext(patch.object(self.runner, 'enqueue_string'))
        self.enterContext(patch.object(self.graph, 'enqueue_string'))
        self.enterContext(patch.object(self.knowledge, 'enqueue_string'))
        return customer, session

    def test_new_faq_and_updated_instructions_work_in_the_same_conversation(self):
        self.publish()
        customer, session = self.conversation()
        self.enterContext(patch.object(self.graph, 'normalize_and_classify', return_value=classification_result([('Pets?', 'information_about_the_cafe', 'pet_policy', None, None)])))
        chain = Mock(invoke=Mock(side_effect=['Outside only.', 'Inside too.']))
        with patch.object(self.knowledge, 'text_chain', return_value=chain) as factory:
            self.assertEqual(self.runner.run_conversation(self.tenant, session, 'Pets?', customer)[0], 'Outside only.')
            self.topic(knowledge='Pets are welcome inside too.')
            TenantJSONDoc.objects.filter(tenant=self.tenant, dtype='response_intents').update(payload='Use a friendly tone.')
            self.publish()
            self.assertEqual(self.runner.run_conversation(self.tenant, session, 'Pets?', customer)[0], 'Inside too.')
            self.assertIn('Use a friendly tone.', factory.call_args.args[0])
            self.assertIn('Pets are welcome inside too.', factory.call_args.args[0])
            self.assertEqual(chain.invoke.call_count, 2)

    def test_disabled_action_blocks_new_requests_pending_tasks_and_checkout_shortcuts(self):
        from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
        from tests.support.ordering import seed_evaluation_policy
        seed_evaluation_policy(self.tenant)
        self.topic(intent='placing_order', sub='add_to_basket')
        self.publish()
        customer, session = self.conversation()
        pending = PlacingOrderIntent(main_query='Coffee', sub_intent='add_to_basket', tenant=self.tenant.pk,
                                      chat_id='runtime-user', follow_up_question=['Which size?'])
        pending.platform = 'website'
        session.set_ongoing_queries([pending], 0)
        self.topic(intent='placing_order', sub='add_to_basket', enabled=False)
        self.publish()
        self.assertIn('add_to_basket', knowledge_cache.get_intent_classification_cache(self.tenant.pk)['placing_order'])
        self.assertFalse(runtime.get_configuration(tenant_id=self.tenant.pk).allows('placing_order', 'add_to_basket'))
        with patch.object(self.graph, 'normalize_and_classify', return_value=classification_result([('Coffee', 'placing_order', 'add_to_basket', None, None)])), \
             patch.object(PlacingOrderIntent, 'process_query') as execute, \
             patch.object(PlacingOrderIntent, 'process_followup') as followup:
            reply, _ = self.runner.run_conversation(self.tenant, session, 'Coffee', customer)
            self.assertIn('currently unavailable', reply)
            self.assertEqual(session.get_ongoing_queries(), ([], None))
            reply, _ = self.runner.run_conversation(self.tenant, session, 'pickup', customer)
            self.assertIn('currently unavailable', reply)
            execute.assert_not_called()
            followup.assert_not_called()

    def test_dashboard_saves_faq_draft_and_publishes_only_current_tenant(self):
        url = reverse('tenant:tenant_knowledge')
        response = self.client.post(url, {'action': 'save_topic', 'intent': 'information_about_the_cafe',
            'sub_intent': 'parking', 'enabled': 'on', 'description': 'Parking questions',
            'examples': 'Can I park?\nWhere is parking?', 'instructions': 'Answer from the parking policy.',
            'knowledge': '"Street parking is available."'})
        self.assertEqual(response.status_code, 302)
        self.assertIsNone(self.document(sub='parking'))
        self.assertFalse(TenantJSONDoc.objects.filter(tenant=self.other).exists())
        response = self.client.post(url, {'action': 'publish', 'version': 0}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'version 1')
        self.assertEqual(self.document(sub='parking')['payload'], 'Street parking is available.')
        self.assertFalse(TenantRuntimeConfiguration.objects.filter(tenant=self.other).exists())
        TenantJSONDoc.objects.filter(tenant=self.tenant, dtype='knowledge', sub_intent='parking').delete()
        response = self.client.post(url, {'action': 'publish', 'version': 1}, follow=True)
        self.assertContains(response, 'required knowledge')
        self.assertEqual(TenantRuntimeConfiguration.objects.get(tenant=self.tenant).version, 1)

    def test_catalog_prices_refresh_without_process_restart(self):
        item = MenuItem.objects.create(tenant=self.tenant, name='Coffee')
        variant = MenuItemVariant.objects.create(menu_item=item, size='Small', price=10)
        menu = knowledge_cache.get_item_pricing_cache()
        self.assertEqual(menu[self.tenant.api_key]['Coffee']['pricing'][str(variant.pk)], '10')
        MenuItemVariant.objects.filter(pk=variant.pk).update(price=20)
        self.assertEqual(menu[self.tenant.api_key]['Coffee']['pricing'][str(variant.pk)], '20')
        MenuItem.objects.filter(pk=item.pk).update(is_available=False)
        self.assertEqual(menu.get(self.tenant.api_key, {}), {})

    def test_required_knowledge_is_supplied_to_the_answerer(self):
        self.topic(required_knowledge=['information_about_the_cafe/support'])
        self.topic(sub='support', knowledge='Call our host for assistance.')
        self.publish()
        self.assertEqual(self.document()['payload']['required_knowledge'],
                         {'information_about_the_cafe/support': 'Call our host for assistance.'})

    def test_answer_context_crosses_topics_but_not_tenants_drafts_or_document_types(self):
        self.topic(sub='contact', knowledge={'phone': '+123-public-phone'})
        self.topic(tenant=self.other, sub='contact', knowledge='Other tenant private phone')
        self.publish()
        self.publish(self.other)
        self.topic(sub='contact', knowledge='Unpublished replacement phone')
        customer, session = self.conversation()
        self.enterContext(patch.object(self.graph, 'normalize_and_classify', return_value=classification_result([
            ('What is your phone?', 'information_about_the_cafe', 'pet_policy', None, None)])))
        chain = Mock(invoke=Mock(return_value='Public phone'))
        with patch.object(self.knowledge, 'text_chain', return_value=chain) as factory:
            self.runner.run_conversation(self.tenant, session, 'What is your phone?', customer)
        prompt = factory.call_args.args[0]
        self.assertIn('+123-public-phone', prompt)
        self.assertNotIn('Other tenant private phone', prompt)
        self.assertNotIn('Unpublished replacement phone', prompt)
        self.assertIn('information_about_the_cafe/contact', prompt)

    def test_retrieved_evidence_and_answer_cache_refresh_together_between_turns(self):
        self.topic(sub='contact', knowledge='Phone version one')
        self.publish()
        self.enterContext(patch.object(self.knowledge, 'enqueue_string'))
        chain = Mock(invoke=Mock(side_effect=['First answer', 'Updated answer']))
        with patch.object(self.knowledge, 'text_chain', return_value=chain) as factory:
            def answer():
                return self.knowledge.generate_response_from_knowledge(
                    self.tenant.api_key, 'pet_policy', 'Phone?', main_intent='information_about_the_cafe',
                    response_profile='cafe_information')
            with runtime.configuration_for_turn(self.tenant.pk):
                self.assertEqual(answer(), 'First answer')
                self.topic(sub='contact', knowledge='Phone version two')
                self.publish()
                self.assertEqual(answer(), 'First answer')
                self.assertIn('Phone version one', factory.call_args.args[0])
            with runtime.configuration_for_turn(self.tenant.pk):
                self.assertEqual(answer(), 'Updated answer')
                self.assertIn('Phone version two', factory.call_args.args[0])
        self.assertEqual(chain.invoke.call_count, 2)

    def test_ordering_information_receives_previous_question_and_complete_answer_profile(self):
        from tests.support.ordering import seed_evaluation_policy
        seed_evaluation_policy(self.tenant)
        self.topic(intent='menu_items', sub='pricing', knowledge={'Tres Leches': '425 INR'})
        self.topic(intent='placing_order', sub='how_to_order', knowledge={
            'steps': ['Choose an item', 'Choose pickup or delivery', 'Check the fee', 'Review payment'],
            'minimum': '700 INR', 'delivery_fee': '100 INR',
        })
        self.publish()
        customer, session = self.conversation()
        classify = self.enterContext(patch.object(self.graph, 'normalize_and_classify'))
        chain = Mock(invoke=Mock(return_value='Ordering information only.'))
        with patch.object(self.knowledge, 'text_chain', return_value=chain) as factory, \
             patch.object(self.knowledge, 'retrieve_knowledge', wraps=self.knowledge.retrieve_knowledge) as retrieve:
            classify.return_value = classification_result([
                ('What does Tres Leches cost?', 'menu_items', 'pricing', None, None)])
            self.runner.run_conversation(self.tenant, session, 'What does Tres Leches cost?', customer)
            classify.return_value = classification_result([
                ('How do I order it?', 'placing_order', 'how_to_order', None, None)])
            self.runner.run_conversation(self.tenant, session, 'How do I order it?', customer)
        self.assertEqual(retrieve.call_args.kwargs['previous_user_message'], 'What does Tres Leches cost?')
        self.assertIn('What does Tres Leches cost?', chain.invoke.call_args.args[0]['input'])
        system = factory.call_args.args[0]
        self.assertIn('ordering information assistant', system)
        self.assertIn('fees, minimums, currencies and conditions', system)
        self.assertIn('Do not ask another question, start an order', system)
        self.assertNotIn('AT MOST 3', system)
        self.assertGreaterEqual(factory.call_args.kwargs['max_tokens'], 1024)

    def test_menu_is_loaded_once_per_turn_and_refreshed_on_the_next_turn(self):
        item = MenuItem.objects.create(tenant=self.tenant, name='Coffee')
        variant = MenuItemVariant.objects.create(menu_item=item, size='Small', price=10)
        with runtime.configuration_for_turn(self.tenant.pk):
            menu = knowledge_cache.get_item_pricing_cache()
            self.assertEqual(menu[self.tenant.api_key]['Coffee']['pricing'][str(variant.pk)], '10')
            with self.assertNumQueries(0):
                self.assertEqual(menu[self.tenant.api_key]['Coffee']['pricing'][str(variant.pk)], '10')
            MenuItemVariant.objects.filter(pk=variant.pk).update(price=20)
        with runtime.configuration_for_turn(self.tenant.pk):
            self.assertEqual(menu[self.tenant.api_key]['Coffee']['pricing'][str(variant.pk)], '20')

    def test_semantic_answer_scope_changes_with_intent_and_published_version(self):
        answers = importlib.import_module('chatbot_core.logic.cafe.prompts.answer_from_knowledge')
        self.publish()
        scopes = []
        with patch.object(answers, 'enqueue_string'), patch.object(answers, 'kb_lookup', return_value=(True, 'Cached', {})) as lookup:
            for main in ['general', 'information_about_the_cafe']:
                with runtime.configuration_for_turn(self.tenant.pk):
                    answers.answer_from_knowledge('Facts', 'Question', tenant_key=str(self.tenant.pk),
                                                   main_intent=main, sub_intent='pet_policy')
                    scopes.append(lookup.call_args.args[1])
            self.publish()
            with runtime.configuration_for_turn(self.tenant.pk):
                answers.answer_from_knowledge('Facts', 'Question', tenant_key=str(self.tenant.pk),
                                               main_intent='information_about_the_cafe', sub_intent='pet_policy')
                scopes.append(lookup.call_args.args[1])
        self.assertEqual(len(set(scopes)), 3)

    def test_legacy_migration_bootstraps_existing_documents(self):
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        migration = importlib.import_module('chatbot_core.migrations.0019_tenantruntimeconfiguration')
        migration.bootstrap_published_documents(apps, SimpleNamespace(connection=connection))
        self.assertEqual(self.document()['payload'], 'Pets are welcome outside.')
        self.assertEqual(TenantRuntimeConfiguration.objects.get(tenant=self.tenant).version, 1)
        with patch.object(runtime, '_cache', OrderedDict()):
            self.assertEqual(self.document()['identity'][-1], 1)

    def test_migration_does_not_publish_invalid_legacy_documents(self):
        from django.apps import apps
        from django.db import connection
        from types import SimpleNamespace
        self.topic(tenant=self.other, knowledge={})
        migration = importlib.import_module('chatbot_core.migrations.0019_tenantruntimeconfiguration')
        migration.bootstrap_published_documents(apps, SimpleNamespace(connection=connection))
        self.assertEqual(TenantRuntimeConfiguration.objects.get(tenant=self.tenant).version, 1)
        unpublished = TenantRuntimeConfiguration.objects.get(tenant=self.other)
        self.assertEqual((unpublished.version, unpublished.documents), (0, []))
        self.assertTrue(TenantJSONDoc.objects.filter(tenant=self.other).exists())

    def test_dashboard_upload_is_a_draft_and_invalid_topic_cannot_execute(self):
        import json
        self.publish()
        response = self.client.post(reverse('tenant:upload_knowledge_prompt'), {
            'dtype': 'knowledge', 'json_blob': json.dumps({'information_about_the_cafe': {'pet_policy': 'Updated by upload.'}}),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.document()['payload'], 'Pets are welcome outside.')
        response = self.client.post(reverse('tenant:tenant_knowledge'), {
            'action': 'save_topic', 'intent': 'placing_order', 'sub_intent': 'issue_refund',
            'enabled': 'on', 'description': 'Refund', 'instructions': 'Issue a refund.', 'knowledge': '{}',
        })
        self.assertContains(response, 'New executable actions require backend code.')
        self.assertFalse(TenantJSONDoc.objects.filter(tenant=self.tenant, sub_intent='issue_refund').exists())
