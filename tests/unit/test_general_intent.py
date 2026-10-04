"""Real general handler and response helper exercised without provider I/O."""
import importlib
from unittest.mock import patch

from django.test import SimpleTestCase

from chatbot_core.logic.cafe.intent_handler.general import GeneralIntent
from tests.support.llm import ProviderHarness

knowledge = importlib.import_module("chatbot_core.logic.cafe.prompts.generate_response_from_knowledge")


class GeneralIntentTests(ProviderHarness, SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.kb = self.enterContext(patch.object(knowledge, "get_knowledge_base_cache", return_value={
            ("tenant", "general", sub): {"payload": {"description": "Respond politely."}}
            for sub in GeneralIntent.SUB_INTENT_NAMES
        }))
        self.enterContext(patch.object(knowledge, "get_intent_prompt_cache", return_value={}))
        self.enterContext(patch.object(knowledge, "enqueue_string"))
        self.arguments = ({}, {}, {}, [], "tenant", None)

    def intent(self, sub="greeting", query="Hello", **kwargs):
        return GeneralIntent(main_query=query, sub_intent=sub, tenant=1, chat_id="user", **kwargs)

    def test_supported_sub_intents_complete_without_pending_questions(self):
        self.payload = "A friendly reply."
        for sub in GeneralIntent.SUB_INTENT_NAMES:
            with self.subTest(sub=sub):
                intent = self.intent(sub)
                self.assertEqual(intent.process_query(*self.arguments), (self.payload, None))
                self.assertTrue(intent.is_complete)
                self.assertEqual(intent.get_followup_question(), "")
                self.assertEqual(self.arguments[:4], ({}, {}, {}, []))

    def test_missing_knowledge_has_sub_intent_specific_fallbacks(self):
        self.kb.return_value = {}
        for sub, fallback in GeneralIntent.FALLBACKS.items():
            self.assertEqual(self.intent(sub).process_query(*self.arguments), (fallback, None))
        self.factory.assert_not_called()

    def test_default_profile_has_no_hardcoded_tenant_branding(self):
        self.payload = "Welcome!"
        for query in ("Hello", "Show the full menu"):
            with self.subTest(query=query):
                self.intent(query=query).process_query(*self.arguments)
                system = self.requests[-1]["messages"][0]["content"]
                self.assertNotIn("Dach & Nona", system)
                self.assertIn("Use ONLY the provided knowledge", system)

    def test_blank_output_is_not_cached_and_a_later_attempt_recovers(self):
        self.payload = " \n "
        with self.assertLogs(knowledge.logger, level="ERROR"):
            self.assertEqual(self.intent().process_query(*self.arguments)[0], GeneralIntent.FALLBACKS["greeting"])
        self.payload = "Welcome!"
        self.assertEqual(self.intent().process_query(*self.arguments)[0], "Welcome!")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.intent().process_query(*self.arguments)[0], "Welcome!")
        self.assertEqual(len(self.requests), 2)

    def test_blank_cached_entry_is_ignored(self):
        self.payload = "Welcome!"
        with patch.object(knowledge.cache, "get", return_value="  "):
            self.assertEqual(self.intent().process_query(*self.arguments)[0], "Welcome!")
        self.assertEqual(len(self.requests), 1)

    def test_provider_failure_falls_back_without_caching_failure(self):
        self.status = 500
        with self.assertLogs(knowledge.logger, level="ERROR"):
            self.assertEqual(self.intent("thanks").process_query(*self.arguments)[0], GeneralIntent.FALLBACKS["thanks"])
        self.status, self.payload = 200, "You're very welcome."
        self.assertEqual(self.intent("thanks").process_query(*self.arguments)[0], self.payload)
        self.assertEqual(len(self.requests), 2)

    def test_unknown_sub_intent_does_not_reach_provider(self):
        with self.assertLogs("chatbot_core.logic.cafe.intent_handler.general", level="WARNING"):
            intent = self.intent("invented")
            self.assertEqual(intent.process_query(*self.arguments), (GeneralIntent.UNKNOWN_RESPONSE, None))
        self.assertTrue(intent.is_complete)
        self.factory.assert_not_called()

    def test_restriction_changes_prompt_and_cache_key(self):
        self.payload = "Hello!"
        self.intent().process_query(*self.arguments)
        restricted = self.intent()
        restricted.promp_restriction = True
        self.assertEqual(restricted.process_query(*self.arguments), ("Hello!", None))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("Do not ask another question", self.requests[1]["messages"][0]["content"])
        restricted.process_query(*self.arguments)
        self.assertEqual(len(self.requests), 2)

    def test_restricted_greeting_fallback_does_not_ask_a_new_question(self):
        self.kb.return_value = {}
        intent = self.intent()
        intent.promp_restriction = True
        self.assertEqual(intent.process_query(*self.arguments), ("Hello!", None))

    def test_followup_uses_incoming_sub_intent_and_previous_response(self):
        self.kb.return_value[("tenant", "general", "goodbye")] = {"payload": {"description": "Farewell instructions."}}
        self.payload = "See you!"
        pending = self.intent(response="Welcome!", follow_up_question=["Old question"])
        incoming = self.intent("goodbye", "Bye")
        incoming.promp_restriction = True
        self.assertEqual(pending.process_followup(incoming, *self.arguments), ("See you!", None))
        system, user = self.requests[0]["messages"]
        self.assertIn("Farewell instructions", system["content"])
        self.assertIn("Do not ask another question", system["content"])
        self.assertIn("Welcome!", user["content"])
        self.assertIn('User: "Bye"', user["content"])
        self.assertEqual(pending.sub_intent, "goodbye")
        self.assertEqual(pending.follow_up_reply, ["Bye"])
        self.assertTrue(pending.is_complete)
        self.assertEqual(pending.follow_up_question, [])
