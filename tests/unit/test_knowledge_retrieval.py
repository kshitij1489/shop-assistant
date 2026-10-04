"""Evidence recall, bounded context and multilingual search failure contracts."""
from collections import OrderedDict
import json
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.cache import cache
from django.test import SimpleTestCase

from chatbot_core import knowledge_retrieval as retrieval
from chatbot_core.runtime_configuration import RuntimeConfiguration


class KnowledgeRetrievalTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.enterContext(patch.object(retrieval, '_indexes', OrderedDict()))
        self.configuration = self.enterContext(patch.object(retrieval, 'get_configuration'))
        self.enterContext(patch.object(retrieval, '_live_menu', return_value=None))
        self.inventory = self.enterContext(patch.object(retrieval, 'inventory_knowledge', return_value={
            'status': 'unknown', 'reason': 'inventory_not_configured', 'records': [],
        }))
        self.provider = self.enterContext(patch.object(retrieval, 'structured_chain',
            return_value=Mock(invoke=Mock(return_value=retrieval.SearchExpansion(search_terms=[])))))

    def configure(self, documents, *, tenant='1', version=1, route='information_about_the_cafe/overview', dependencies=()):
        intent, topic = route.split('/')
        docs = [{'dtype': 'knowledge', 'intent': key.split('/')[0], 'sub_intent': key.split('/')[1], 'payload': value}
                for key, value in documents.items()]
        docs.append({'dtype': 'intent_classification', 'intent': intent, 'sub_intent': topic,
                     'payload': {'description': 'Information', 'required_knowledge': list(dependencies)}})
        self.configuration.return_value = RuntimeConfiguration(tenant, f'key-{tenant}', tenant, version, docs)

    def retrieve(self, query, route='information_about_the_cafe/overview', **kwargs):
        return retrieval.retrieve_knowledge(self.configuration.return_value.api_key, *route.split('/'), query, **kwargs)

    def test_audited_facts_are_available_across_topics_without_dependencies(self):
        root = Path(__file__).resolve().parents[2] / 'test_data'
        documents = {}
        for file in ('01_cafe_knowledge.json', '02_menu_knowledge.json', '03_ordering_knowledge.json'):
            for intent, topics in json.loads((root / file).read_text()).items():
                documents.update({f'{intent}/{topic}': value for topic, value in topics.items()})
        cases = [
            ('information_about_the_cafe/team_and_policy', 'How do I contact you?', 'cafe_phone'),
            ('menu_items/allergens', 'Is Tres Leches eggless?', 'explicitly_eggless'),
            ('menu_items/pricing', 'What category and price is Pistachio Ice Cream?', 'Comfort Classics Ice Creams'),
            ('information_about_the_cafe/team_and_policy', 'Who owns the business?', 'Subira Desserts'),
            ('placing_order/order_channels_and_modes', 'What are delivery fees and minimums?', 'minimum_order_inr'),
            ('information_about_the_cafe/team_and_policy', 'आपसे संपर्क कैसे करें?', '+919220840600'),
        ]
        for route, query, fact in cases:
            self.configure(documents, route=route)
            result = self.retrieve(query, route)['payload']
            self.assertIn(fact, retrieval.encoded(result))
            self.assertEqual(result['coverage'], 'complete')
        self.provider.assert_not_called()

    def test_large_unrelated_corpus_does_not_hide_cross_topic_evidence(self):
        documents = {f'information_about_the_cafe/topic_{i}': {'description': f'Archive record {i} of routine events.'}
                     for i in range(3000)}
        documents['information_about_the_cafe/overview'] = 'General company introduction.'
        documents['information_about_the_cafe/billing'] = {'minimum': 'The minimum service fee is 725 credits.'}
        self.configure(documents)
        result = self.retrieve('What is the minimum service fee?')['payload']
        self.assertIn('725 credits', retrieval.encoded(result))
        self.assertEqual(result['coverage'], 'partial')
        self.assertLessEqual(len(retrieval.encoded(result).encode()), retrieval.CONTEXT_BYTES)
        self.assertLess(len(result['fragments']), 3000)

    def test_structured_subjects_and_qualifiers_survive_fragment_selection(self):
        records = {f'Product {i}': {'claim': 'eggless', 'note': 'Shared equipment; cross-contact not verified.'}
                   for i in range(400)}
        self.configure({'information_about_the_cafe/diet': {
            'as_of': '2026-09-01', 'note': 'Published labels only, not an allergen guarantee.', 'items': records}})
        with patch.object(retrieval, 'CONTEXT_BYTES', 2500):
            result = self.retrieve('Product 137 eggless')['payload']
        rendered = retrieval.encoded(result)
        for text in ('Product 137', 'Shared equipment', 'not an allergen guarantee', '2026-09-01'):
            self.assertIn(text, rendered)
        self.assertEqual(result['coverage'], 'partial')
        self.assertLessEqual(len(rendered.encode()), 2500)

    def test_multilingual_expansion_is_search_only_and_cached_with_tenant_and_version(self):
        documents = {f'information_about_the_cafe/archive_{i}': 'Historical record ' + '旧记录 ' * 80 for i in range(90)}
        documents['information_about_the_cafe/contact'] = {'telephone': '+123456789'}
        self.configure(documents)
        self.provider.return_value.invoke.return_value = retrieval.SearchExpansion(
            search_terms=['contact telephone number', 'NOT A DOCUMENTED FACT'])
        query = 'आपका फ़ोन नंबर क्या है?'
        result = self.retrieve(query)['payload']
        self.assertIn('+123456789', retrieval.encoded(result))
        self.assertNotIn('NOT A DOCUMENTED FACT', retrieval.encoded(result))
        self.retrieve(query)
        self.assertEqual(self.provider.call_count, 1)
        self.configure(documents, tenant='2')
        self.retrieve(query)
        self.configure(documents, tenant='2', version=2)
        self.retrieve(query)
        self.assertEqual(self.provider.call_count, 3)

    def test_expansion_failure_keeps_literal_matches_and_marks_incomplete_search(self):
        self.configure({f'information_about_the_cafe/topic_{i}': f'Identifier X{i} ' + 'facts ' * 500 for i in range(40)})
        self.provider.side_effect = TimeoutError('provider unavailable')
        with self.assertLogs(retrieval.logger, level='WARNING'):
            result = self.retrieve('X37')['payload']
        self.assertIn('X37', retrieval.encoded(result))
        self.assertTrue(result['search_degraded'])
        self.assertEqual(result['coverage'], 'partial')

    def test_prior_question_resolves_search_references_without_using_assistant_claims(self):
        self.configure({f'information_about_the_cafe/topic_{i}': f'Service {i} ' + 'details ' * 300 for i in range(40)})
        self.retrieve('How much?', previous_user_message='Tell me about Service 32')
        self.assertIn('Service 32', self.provider.return_value.invoke.call_args.args[0]['input'])

    def test_english_expansion_reused_across_languages_without_losing_original_literals(self):
        documents = {f'information_about_the_cafe/archive_{i}': 'Archived facts ' * 300 for i in range(40)}
        documents['information_about_the_cafe/contact'] = {'telephone': '+123456789'}
        documents['information_about_the_cafe/special'] = {'商店甲': 'Special branch code 725'}
        self.configure(documents)
        rewrite = 'What is the telephone number?'
        first = self.retrieve('आपका फ़ोन नंबर क्या है?', rephrased_sentence=rewrite)['payload']
        second = self.retrieve('商店甲 teléfono?', rephrased_sentence=rewrite)['payload']
        self.assertIn('+123456789', retrieval.encoded(first))
        self.assertIn('Special branch code 725', retrieval.encoded(second))
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.provider.return_value.invoke.call_args.args[0]['input'], rewrite)
        self.configure(documents, tenant='2')
        self.retrieve('teléfono?', rephrased_sentence=rewrite)
        self.configure(documents, tenant='2', version=2)
        self.retrieve('teléfono?', rephrased_sentence=rewrite)
        self.assertEqual(self.provider.call_count, 3)

    def test_english_rewrite_keeps_retrieval_useful_when_expansion_fails(self):
        self.configure({f'information_about_the_cafe/topic_{i}': f'Service {i} ' + 'facts ' * 300 for i in range(40)})
        self.provider.side_effect = TimeoutError('provider unavailable')
        with self.assertLogs(retrieval.logger, 'WARNING'):
            result = self.retrieve('उसके बारे में बताओ', rephrased_sentence='Tell me about Service 37')['payload']
        self.assertIn('Service 37', retrieval.encoded(result))
        self.assertTrue(result['search_degraded'])

    def test_disabled_sources_and_unpublished_or_disabled_routes_are_not_retrieved(self):
        self.configure({'information_about_the_cafe/overview': 'Public facts',
                        'information_about_the_cafe/retired': 'Retired secret'})
        configuration = self.configuration.return_value
        configuration.documents.append({'dtype': 'intent_classification', 'intent': 'information_about_the_cafe',
                                         'sub_intent': 'retired', 'payload': {'enabled': False}})
        self.assertNotIn('Retired secret', retrieval.encoded(self.retrieve('secret')))
        self.assertIsNone(self.retrieve('secret', 'information_about_the_cafe/retired'))
        self.configure({'information_about_the_cafe/overview': 'Unpublished facts'}, version=0)
        self.assertIsNone(self.retrieve('facts'))

    def test_sources_without_routes_and_dependencies_are_evidence_not_executable_routes(self):
        self.configure({'information_about_the_cafe/overview': 'Overview',
                        'menu_items/menu_category': {'Coffee': 'Drinks'}},
                       dependencies=['menu_items/menu_category'])
        self.assertIn('Drinks', retrieval.encoded(self.retrieve('Coffee category?')))
        self.assertIsNone(self.retrieve('Coffee category?', 'menu_items/menu_category'))

    def test_tenants_versions_and_cache_eviction_do_not_change_evidence(self):
        with patch.object(retrieval, 'MAX_INDEXES', 1):
            self.configure({'information_about_the_cafe/overview': 'First tenant'})
            first = self.retrieve('facts')
            self.configure({'information_about_the_cafe/overview': 'Second tenant'}, tenant='2')
            second = self.retrieve('facts')
            self.assertNotIn('First tenant', retrieval.encoded(second))
            self.configure({'information_about_the_cafe/overview': 'Updated first tenant'}, version=2)
            updated = self.retrieve('facts')
            self.assertNotEqual(first['identity'], updated['identity'])
            self.assertIn('Updated first tenant', retrieval.encoded(updated))
            self.assertEqual(len(retrieval._indexes), 1)

    def test_oversize_index_is_usable_without_retaining_it_in_worker_cache(self):
        self.configure({'information_about_the_cafe/overview': 'Contact telephone 12345'})
        with patch.object(retrieval, 'MAX_INDEX_BYTES', 1):
            self.assertIn('12345', retrieval.encoded(self.retrieve('telephone')))
        self.assertEqual(len(retrieval._indexes), 0)

    def test_returned_evidence_cannot_mutate_the_cached_index(self):
        self.configure({'information_about_the_cafe/overview': {'telephone': '12345'}})
        first = self.retrieve('telephone')
        first['payload']['fragments'][0]['value']['telephone'] = 'tampered'
        self.assertNotIn('tampered', retrieval.encoded(self.retrieve('telephone')))

    def test_no_lexical_matches_does_not_report_complete_coverage(self):
        self.configure({f'information_about_the_cafe/topic_{i}': 'Archived facts ' * 300 for i in range(40)})
        result = self.retrieve('zzzzzz')['payload']
        self.assertEqual(result['fragments'], [])
        self.assertEqual(result['coverage'], 'partial')

    def test_large_inventory_keeps_relevant_stock_within_the_shared_context_budget(self):
        self.configure({f'information_about_the_cafe/topic_{i}': 'Archived facts ' * 300 for i in range(40)})
        self.inventory.return_value = {
            'status': 'checked', 'records': [
                {'item_name': f'Flavor {i}', 'variant_name': 'Regular', 'status': 'in_stock',
                 'available_units': i, 'stock_pool_id': f'pool-{i}'} for i in range(1000)],
        }
        self.provider.return_value.invoke.return_value = retrieval.SearchExpansion(search_terms=['Flavor 937'])
        with patch.object(retrieval, 'CONTEXT_BYTES', 3000):
            result = self.retrieve('क्या यह स्टॉक में है?', previous_user_message='Flavor 937')['payload']
        self.assertIn('Flavor 937', retrieval.encoded(result['inventory']))
        self.assertEqual(result['inventory']['coverage'], 'partial')
        self.assertLessEqual(len(retrieval.encoded(result).encode()), 3000)
        self.assertEqual(self.provider.call_count, 1)

    def test_inventory_read_is_fresh_even_when_document_index_is_reused(self):
        self.configure({'information_about_the_cafe/overview': 'Public facts'})
        self.inventory.return_value = {'status': 'checked', 'records': [
            {'item_name': 'Vanilla', 'status': 'in_stock', 'available_units': 1}]}
        first = self.retrieve('Vanilla')['payload']
        self.inventory.return_value = {'status': 'checked', 'records': [
            {'item_name': 'Vanilla', 'status': 'out_of_stock', 'available_units': 0}]}
        second = self.retrieve('Vanilla')['payload']
        self.assertNotEqual(first['inventory'], second['inventory'])
        self.assertEqual(len(retrieval._indexes), 1)
        self.assertEqual(self.inventory.call_count, 2)
        self.provider.assert_not_called()
