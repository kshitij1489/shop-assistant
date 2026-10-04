"""Shared reference context stays bounded and isolated across route changes."""
from types import SimpleNamespace

from django.test import SimpleTestCase

from chatbot_core.logic.cafe.knowledge_context import (
    MAX_QUESTION_CHARS, MAX_REPLY_CHARS, previous_knowledge_context,
)


class KnowledgeContextTests(SimpleTestCase):
    def setUp(self):
        self.intent = SimpleNamespace(tenant=1, chat_id='guest', platform='website')
        self.previous = {'tenant': '1', 'chat_id': 'guest', 'platform': 'website',
                         'intent_type': 'placing_order', 'main_query': 'How do I order a tart?',
                         'response': 'Ordering instructions.'}

    def test_only_current_scope_and_immediately_previous_exchange_are_eligible(self):
        self.assertEqual(previous_knowledge_context(self.intent, [{'query_obj': self.previous}]), {
            'previous_user_message': 'How do I order a tart?', 'system_log_message': 'Ordering instructions.',
        })
        for override in ({'tenant': 2}, {'chat_id': 'another'}, {'platform': 'telegram'},
                         {'platform': None}, {'intent_type': 'out_of_context'}, {'intent_type': 'general'}):
            history = [{'query_obj': self.previous}, {'query_obj': self.previous | override}]
            self.assertEqual(previous_knowledge_context(self.intent, history), {})
        self.intent.platform = None
        self.assertEqual(previous_knowledge_context(self.intent, [{'query_obj': self.previous | {'platform': None}}]), {})

    def test_malformed_or_oversize_history_does_not_escape_reference_budget(self):
        for entry in (None, [], {}, {'query_obj': []}, {'query_obj': None}):
            self.assertEqual(previous_knowledge_context(self.intent, [entry]), {})
        previous = self.previous | {'main_query': 'x' * 10000, 'response': 'y' * 10000}
        result = previous_knowledge_context(self.intent, [{'query_obj': previous}])
        self.assertEqual(len(result['previous_user_message']), MAX_QUESTION_CHARS)
        self.assertEqual(len(result['system_log_message']), MAX_REPLY_CHARS)
        self.assertEqual(len(previous['main_query']), 10000)
        previous.update(main_query=[], response={'untrusted': 'object'})
        self.assertEqual(previous_knowledge_context(self.intent, [{'query_obj': previous}]), {})
