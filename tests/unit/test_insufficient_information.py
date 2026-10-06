"""Real clarification handler and LangGraph transitions with offline model I/O."""
from tests.support.runtime import classification_result
import importlib
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler.insufficient_information import InsufficientInformationIntent
from tests.support.llm import ProviderHarness

clarification = importlib.import_module("chatbot_core.logic.cafe.prompts.clarify_user_message")


class ClarificationHarness(ProviderHarness):
    def setUp(self):
        super().setUp()
        self.payload = "Could you rephrase your request?"
        self.arguments = ({"items": ["latte"]}, {"city": "Delhi"}, {"payment": False}, [], "tenant-1", None)

    def intent(self, query="huh?", sub="insufficient_information", **kwargs):
        intent = InsufficientInformationIntent(main_query=query, sub_intent=sub,
                                              tenant=1, chat_id="user", **kwargs)
        intent.platform = "telegram"
        return intent


class InsufficientInformationTests(ClarificationHarness, SimpleTestCase):
    def test_clarification_needs_no_knowledge_and_preserves_business_state(self):
        before = deepcopy(self.arguments)
        intent = self.intent()
        with patch("chatbot_core.knowledge_cache.get_knowledge_base_cache", side_effect=AssertionError("No KB needed")):
            self.assertEqual(intent.process_query(*self.arguments), (self.payload, None))
        self.assertFalse(intent.is_complete)
        self.assertEqual(intent.follow_up_question, [self.payload])
        self.assertEqual(self.arguments, before)
        self.assertIsNone(intent.handoff_to)

    def test_followup_uses_latest_message_and_previous_exchange(self):
        intent = self.intent()
        intent.process_query(*self.arguments)
        intent.process_followup(self.intent('that {thing} "please"'), *self.arguments)
        context = json.loads(self.requests[-1]["messages"][1]["content"])
        self.assertEqual(context, {"previous_user_message": "huh?",
                                  "previous_assistant_message": self.payload,
                                  "latest_user_message": 'that {thing} "please"'})
        self.assertEqual(intent.main_query, 'that {thing} "please"')
        self.assertEqual(intent.follow_up_reply, [intent.main_query])

    def test_blank_or_non_text_input_uses_fallback_without_provider(self):
        for query in ("", " \n ", None, 12, []):
            with self.subTest(query=query):
                intent = self.intent(query)
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertFalse(intent.is_complete)
                self.assertEqual(intent.get_followup_question(), intent.FALLBACK_RESPONSE)
        self.factory.assert_not_called()

    def test_unknown_sub_intents_complete_without_provider(self):
        for sub in ("wrong", "", None, []):
            with self.subTest(sub=sub), self.assertLogs(level="WARNING"):
                intent = self.intent(sub=sub)
                self.assertEqual(intent.process_query(*self.arguments), (intent.EXHAUSTED_RESPONSE, None))
                self.assertTrue(intent.is_complete)
                self.assertEqual(intent.follow_up_question, [])
        self.factory.assert_not_called()

    def test_sub_intent_normalization(self):
        intent = self.intent(sub=" Insufficient_Information ")
        self.assertEqual(intent.process_query(*self.arguments), (self.payload, None))
        self.assertEqual(intent.sub_intent, "insufficient_information")

    def test_restriction_completes_without_asking_or_calling_provider(self):
        intent = self.intent(follow_up_question=["Old question"])
        incoming = self.intent()
        incoming.promp_restriction = True
        self.assertEqual(intent.process_followup(incoming, *self.arguments), (intent.RESTRICTED_RESPONSE, None))
        self.assertTrue(intent.is_complete)
        self.assertEqual(intent.follow_up_question, [])
        self.factory.assert_not_called()

    def test_provider_failure_and_blank_output_fall_back_without_caching(self):
        for status, payload in ((500, "unavailable"), (200, " \n ")):
            self.status, self.payload = status, payload
            with self.assertLogs(clarification.logger, level="ERROR"):
                intent = self.intent()
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertEqual(intent.get_followup_question(), intent.FALLBACK_RESPONSE)
        self.payload = "What would you like help with?"
        self.assertEqual(self.intent().process_query(*self.arguments)[0], self.payload)
        self.assertEqual(len(self.requests), 3)


class InsufficientInformationGraphTests(ClarificationHarness, SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")
        cls.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")

    def setUp(self):
        super().setUp()
        from chatbot_core.logic.cafe.session import memory
        from chatbot_core.logic.cafe.intent_handler.general import GeneralIntent
        from tests.support.conversations import ScriptedIntent
        self.scripted = ScriptedIntent
        self.scripted.operations = []
        resolve = {"insufficient_information": InsufficientInformationIntent,
                   "general": GeneralIntent, "scripted": ScriptedIntent}.__getitem__
        self.enterContext(patch.object(self.graph, "get_intent", side_effect=resolve))
        self.enterContext(patch.object(base, "get_intent", side_effect=resolve))
        self.enterContext(patch.object(memory, "_session_data", {}))
        self.session = memory.MemorySessionStore("user", tenant_id=1, platform="telegram")
        self.tenant = SimpleNamespace(id=1, pk=1, api_key="tenant-1")
        self.enterContext(patch.object(self.runner, "get_chat_ongoing_session", return_value=object()))
        from tests.support.runtime import install_runtime_fixture
        install_runtime_fixture(self)
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.classify = self.enterContext(patch.object(self.graph, "normalize_and_classify"))

    def send(self, text='huh?', *, reply_to=None, clarification='Could you clarify?', route=None, empty=False):
        route = route or ('insufficient_information', 'insufficient_information')
        self.classify.return_value = classification_result([] if empty else [(text, *route, reply_to, clarification)])
        return self.runner.run_conversation(self.tenant, self.session, text)

    def test_clarification_reloads_and_expires_without_handler_or_business_mutation(self):
        before = self.session.get_basket().to_dict()
        reply_to = None
        for turn in range(3):
            reply, _ = self.send(reply_to=reply_to)
            pending, index = self.session.get_ongoing_queries()
            if turn < 2:
                self.assertEqual(reply, 'Could you clarify?')
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].ignored_count, turn + 1)
                self.assertEqual(pending[0].basket_item['clarification_budget']['delivered'], turn + 1)
                reply_to = str(pending[0].query_id)
                self.session.set_ongoing_queries([base.BaseIntent.from_dict(pending[0].to_dict())], index)
            else:
                self.assertIn('start again', reply)
                self.assertEqual(pending, [])
                ended = self.session.get_history()[-1]['query_obj']
                self.assertEqual(ended['outcome'], 'terminal_rejection')
                self.assertEqual(ended['follow_up_question'], [])
                self.assertFalse(self.session.get_checklist().get('last_assistant_question'))
        self.assertEqual(before, self.session.get_basket().to_dict())
        self.factory.assert_not_called()

    def test_old_session_over_budget_closes_at_delivery_without_handler_call(self):
        pending = self.intent(follow_up_question=['Old question?'] * 4, query_id='legacy')
        self.session.set_ongoing_queries([pending], 0)
        before = self.session.get_basket().to_dict()
        with patch.object(InsufficientInformationIntent, 'process_followup') as handler:
            reply, _ = self.send(reply_to='legacy')
        self.assertIn('start again', reply)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        ended = self.session.get_history()[-1]['query_obj']
        self.assertEqual(ended['outcome'], 'terminal_rejection')
        self.assertEqual(ended['follow_up_question'], [])
        self.assertEqual(self.session.get_basket().to_dict(), before)
        handler.assert_not_called()
        self.factory.assert_not_called()

    def test_empty_classification_preserves_pending_and_business_state(self):
        self.send()
        before = deepcopy(self.session.read_snapshot())
        reply, basket = self.send(empty=True)
        self.assertIn('ask again', reply)
        self.assertIsNone(basket)
        after = self.session.read_snapshot()
        for key in ('ongoing_query_queue','basket','checklist','chat_history'):
            self.assertEqual(before[key], after[key])

    def test_cancel_uses_explicit_pending_id(self):
        self.send()
        pending = self.session.get_ongoing_queries()[0][0]
        reply, _ = self.send('stop this request', reply_to=str(pending.query_id),
                             clarification=None, route=('general','cancel_and_abort'))
        self.assertIn('Stopped', reply)
        self.assertEqual(self.session.get_ongoing_queries(), ([],None))

    def test_independent_request_resolves_generic_clarification(self):
        self.send()
        reply, _ = self.send('save', clarification=None, route=('scripted','save'))
        self.assertEqual(reply, 'save reply')
        self.assertEqual(self.session.get_delivery_address(), {'city':'Delhi'})
        self.assertEqual(self.session.get_ongoing_queries(), ([],None))

    def test_detour_and_wait_preserve_budget_without_repeating_question(self):
        self.send()
        before = self.session.get_ongoing_queries()[0][0].to_dict()
        with patch('chatbot_core.logic.cafe.intent_handler.general.generate_response_from_knowledge', return_value='Take your time.'):
            reply, _ = self.send('wait', clarification=None, route=('general','wait'))
        self.assertEqual(reply, 'Take your time.')
        self.assertEqual(self.session.get_ongoing_queries()[0][0].to_dict(), before)

    def test_known_operation_clarifies_without_executing_prefix(self):
        reply, _ = self.send('Add a latte once we choose the size', clarification='Which size?', route=('scripted','ask'))
        pending = self.session.get_ongoing_queries()[0][0]
        self.assertEqual(reply, 'Which size?')
        self.assertEqual(self.scripted.operations, [])
        reply, _ = self.send('large', reply_to=str(pending.query_id), clarification=None, route=('scripted','large'))
        self.assertEqual(reply,'followup reply')
        self.assertEqual(self.session.get_ongoing_queries(),([],None))

    def test_redis_reload_preserves_clarification_budget(self):
        from chatbot_core.logic.cafe.session import redis_session
        from tests.support.conversations import FakeRedis
        self.enterContext(patch.object(redis_session, '_redis', FakeRedis()))
        self.session = redis_session.RedisSessionStore('user', tenant_id=1, platform='telegram')
        self.test_clarification_reloads_and_expires_without_handler_or_business_mutation()
