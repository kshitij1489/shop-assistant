"""Scope redirects through real response chains and LangGraph, with offline I/O."""
from tests.support.runtime import classification_result
import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch, ANY

from django.test import SimpleTestCase

from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler.out_of_context import OutOfContextIntent
from tests.support.llm import ProviderHarness

knowledge = importlib.import_module("chatbot_core.logic.cafe.prompts.generate_response_from_knowledge")


class OutOfContextHarness(ProviderHarness):
    def setUp(self):
        super().setUp()
        self.payload = "I can help with café information, menu items, and orders."
        self.kb = self.enterContext(patch.object(knowledge, "get_knowledge_base_cache", return_value={
            ("tenant-1", "out_of_context", "out_of_scope"): {"payload": {"description": "Respond politely."}},
        }))
        self.enterContext(patch.object(knowledge, "get_intent_prompt_cache", return_value={}))
        self.enterContext(patch.object(knowledge, "enqueue_string"))
        self.arguments = ({"items": ["latte"]}, {"city": "Delhi"}, {"payment": False}, [], "tenant-1", None)

    def intent(self, query="Write some code", sub="out_of_scope", **kwargs):
        intent = OutOfContextIntent(main_query=query, sub_intent=sub, tenant=1, chat_id="user", **kwargs)
        intent.platform = "telegram"
        return intent


class OutOfContextTests(OutOfContextHarness, SimpleTestCase):
    def test_reply_completes_without_questions_handoff_or_business_mutation(self):
        before = deepcopy(self.arguments)
        intent = self.intent(follow_up_question=["Old reply?"])
        self.assertEqual(intent.process_query(*self.arguments), (self.payload, None))
        self.assertEqual(intent.response, self.payload)
        self.assertTrue(intent.is_complete)
        self.assertEqual(intent.follow_up_question, [])
        self.assertIsNone(intent.handoff_to)
        self.assertEqual(self.arguments, before)

    def test_invalid_sub_intents_and_empty_messages_skip_provider(self):
        for sub in ("unknown", "", None, ["out_of_scope"]):
            with self.subTest(sub=sub), self.assertLogs(level="WARNING"):
                intent = self.intent(sub=sub)
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertTrue(intent.is_complete)
        for query in ("", " \n ", None, 12, []):
            intent = self.intent(query=query)
            self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
        self.factory.assert_not_called()

    def test_normalizes_sub_intent(self):
        intent = self.intent(sub=" Out_Of_Scope ")
        self.assertEqual(intent.process_query(*self.arguments), (self.payload, None))
        self.assertEqual(intent.sub_intent, "out_of_scope")

    def test_absent_or_empty_knowledge_uses_scope_fallback(self):
        for entry in (None, {}, {"payload": None}, {"payload": {}}, {"payload": []}, {"payload": "  "}):
            self.kb.return_value = {("tenant-1", "out_of_context", "out_of_scope"): entry}
            self.assertEqual(self.intent().process_query(*self.arguments)[0], OutOfContextIntent.FALLBACK_RESPONSE)
        self.factory.assert_not_called()

    def test_blank_output_and_provider_failure_do_not_poison_cache(self):
        for status, payload in ((500, "unavailable"), (200, " \n ")):
            self.status, self.payload = status, payload
            with self.assertLogs(knowledge.logger, level="ERROR"):
                intent = self.intent()
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertTrue(intent.is_complete)
        self.payload = "I can help with the café."
        self.assertEqual(self.intent().process_query(*self.arguments)[0], self.payload)
        self.assertEqual(self.intent().process_query(*self.arguments)[0], self.payload)
        self.assertEqual(len(self.requests), 3)

    def test_knowledge_or_cache_outage_still_completes(self):
        for target in (self.kb, knowledge.cache):
            context = (patch.object(target, "side_effect", RuntimeError("unavailable")) if target is self.kb
                       else patch.object(target, "get", side_effect=RuntimeError("unavailable")))
            with context, self.assertLogs(level="ERROR"):
                intent = self.intent(follow_up_question=["Old question?"])
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertTrue(intent.is_complete)
                self.assertEqual(intent.follow_up_question, [])

    def test_scope_prompt_overrides_generic_menu_rules_and_separates_cache(self):
        query = 'Give me the full menu for another restaurant {please}'
        knowledge.generate_response_from_knowledge("tenant-1", "out_of_scope", query, main_intent="out_of_context")
        intent = self.intent(query=query)
        intent.process_query(*self.arguments)
        self.assertEqual(len(self.requests), 2)
        system = self.requests[-1]["messages"][0]["content"]
        self.assertIn("Do not answer or carry out the out-of-scope request", system)
        self.assertIn("Do not ask another question", system)
        self.assertIn("Use the user's language", system)
        self.assertNotIn("provide the full item list", system)
        self.assertNotIn("AT MOST 3", system)
        self.assertIn(query, self.requests[-1]["messages"][1]["content"])
        intent.promp_restriction = True
        intent.process_query(*self.arguments)
        self.assertEqual(len(self.requests), 3)

    def test_followup_uses_current_message_classification_and_restriction(self):
        pending = self.intent(follow_up_question=["Old reply?"])
        incoming = self.intent("Tell me a joke")
        incoming.promp_restriction = True
        self.assertEqual(pending.process_followup(incoming, *self.arguments), (self.payload, None))
        self.assertEqual(pending.main_query, incoming.main_query)
        self.assertEqual(pending.follow_up_reply, [incoming.main_query])
        self.assertTrue(pending.promp_restriction)
        self.assertEqual(pending.follow_up_question, [])
        self.assertIn(incoming.main_query, self.requests[-1]["messages"][1]["content"])
        with self.assertLogs(level="WARNING"):
            self.assertEqual(pending.process_followup(self.intent(sub="pricing"), *self.arguments)[0],
                             pending.FALLBACK_RESPONSE)
        self.assertEqual(len(self.requests), 1)


class OutOfContextGraphTests(OutOfContextHarness, SimpleTestCase):
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
        from chatbot_core.logic.cafe.intent_handler.insufficient_information import InsufficientInformationIntent
        from tests.support.conversations import ScriptedIntent
        self.scripted = ScriptedIntent
        self.scripted.operations = []
        resolve = {"out_of_context": OutOfContextIntent, "scripted": ScriptedIntent,
                   "insufficient_information": InsufficientInformationIntent}.__getitem__
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
        self.reply_to_pending = False

    def send(self, rows=None):
        rows = rows or [("Write some code", "out_of_context", "out_of_scope", None, None)]
        query = " and ".join(row[0] for row in rows)
        pending, _ = self.session.get_ongoing_queries()
        if pending and rows[0][1] == 'scripted' and self.reply_to_pending:
            rows = [(*row[:3], str(pending[-1].query_id), None) for row in rows]
        self.classify.return_value = classification_result(rows)
        return self.runner.run_conversation(self.tenant, self.session, query)

    def test_one_shot_is_saved_in_history_without_pending_work(self):
        self.assertEqual(self.send()[0], self.payload)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        recorded = self.session.get_history()[-1]["query_obj"]
        self.assertEqual(recorded["response"], self.payload)
        self.assertTrue(recorded["is_complete"])
        self.assertEqual(recorded["follow_up_question"], [])

    def test_interruption_preserves_task_and_user_can_resume(self):
        self.send([("latte", "scripted", "ask", None, None)])
        before = self.session.get_ongoing_queries()[0][0].to_dict()
        for _ in range(2):
            self.assertEqual(self.send()[0], self.payload)
            pending, index = self.session.get_ongoing_queries()
            self.assertEqual((pending[0].to_dict(), index), (before, 0))
        self.reply_to_pending = True
        self.assertEqual(self.send([("large", "scripted", "large", None, None)])[0], "followup reply")
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_mixed_message_preserves_new_task_and_handoff_in_both_orders(self):
        off_topic = ("Write some code", "out_of_context", "out_of_scope", None, None)
        for sub, question in (("ask", "Which size?"), ("handoff", "Confirm order?")):
            for rows in ([off_topic, ("latte", "scripted", sub, None, None)], [("latte", "scripted", sub, None, None), off_topic]):
                self.session.set_ongoing_queries([], None)
                response, _ = self.send(rows)
                self.assertEqual(response.count(question), 1)
                self.assertIn(self.payload, response)
                pending, index = self.session.get_ongoing_queries()
                self.assertEqual((len(pending), index, pending[0].intent_type), (1, 0, "scripted"))

    def test_missing_knowledge_fallback_preserves_pending_task(self):
        self.send([("latte", "scripted", "ask", None, None)])
        self.kb.return_value = {}
        self.assertEqual(self.send()[0], OutOfContextIntent.FALLBACK_RESPONSE)
        self.assertEqual(self.session.get_ongoing_queries()[1], 0)

    def test_stale_scope_entries_cannot_capture_new_requests_or_splitter_context(self):
        self.session.set_ongoing_queries([self.intent(follow_up_question=["Old reply?"])], 0)
        self.assertEqual(self.send([("save address", "scripted", "save", None, None)])[0], "save reply")
        self.classify.assert_called_once_with("save address", "", "", tenant_key="1", conversation_context=ANY)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual(self.session.get_delivery_address(), {"city": "Delhi"})

    def test_removing_stale_entries_remaps_real_followup_index(self):
        self.send([("latte", "scripted", "ask", None, None)])
        pending, _ = self.session.get_ongoing_queries()
        self.session.set_ongoing_queries([self.intent(follow_up_question=["Old reply?"]), *pending], 1)
        self.assertEqual(self.send()[0], self.payload)
        self.classify.assert_called_with("Write some code", "Which size?", "latte", tenant_key="1", conversation_context=ANY)
        self.assertEqual(self.session.get_ongoing_queries()[1], 0)

    def test_interruption_preserves_clarification_budget(self):
        from chatbot_core.logic.cafe.intent_handler.insufficient_information import InsufficientInformationIntent
        pending = InsufficientInformationIntent(main_query="huh", sub_intent="insufficient_information",
                                              tenant=1, chat_id="user", follow_up_question=["Please rephrase?"])
        pending.platform = "telegram"
        self.session.set_ongoing_queries([pending], 0)
        self.assertEqual(self.send()[0], self.payload)
        restored = self.session.get_ongoing_queries()[0][0]
        self.assertEqual(restored.follow_up_question, ["Please rephrase?"])
        self.assertEqual(restored.ignored_count, 0)

    def test_redis_roundtrip_preserves_task_across_interruption(self):
        from chatbot_core.logic.cafe.session import redis_session
        from tests.support.conversations import FakeRedis
        self.enterContext(patch.object(redis_session, "_redis", FakeRedis()))
        self.session = redis_session.RedisSessionStore("user", tenant_id=1, platform="telegram")
        self.test_interruption_preserves_task_and_user_can_resume()
