"""Café information contracts with real chains and graph, without external I/O."""
from tests.support.runtime import classification_result, classification_rows
import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler.information_about_the_cafe import InformationAboutCafeIntent
from tests.support.llm import ProviderHarness

knowledge = importlib.import_module("chatbot_core.logic.cafe.prompts.generate_response_from_knowledge")


class CafeInformationHarness(ProviderHarness):
    def setUp(self):
        super().setUp()
        self.payload = "The café opens at 9 am."
        self.kb = self.enterContext(patch.object(knowledge, "get_knowledge_base_cache", return_value={
            ("tenant-1", "information_about_the_cafe", sub): {"payload": {"details": f"Facts for {sub}"}}
            for sub in InformationAboutCafeIntent.SUB_INTENT_NAMES
        }))
        self.prompts = self.enterContext(patch.object(knowledge, "get_intent_prompt_cache", return_value={}))
        self.enterContext(patch.object(knowledge, "retrieve_knowledge", side_effect=
            lambda api, intent, sub, query, **kw: self.kb.return_value.get((api, intent, sub))))
        self.enterContext(patch.object(knowledge, "enqueue_string"))
        self.arguments = ({"items": ["latte"]}, {"city": "Delhi"}, {"payment": False}, [], "tenant-1", None)

    def intent(self, sub="location_and_hours", query="When do you open?", **kwargs):
        intent = InformationAboutCafeIntent(
            main_query=query, sub_intent=sub, tenant=1, chat_id="user", **kwargs,
        )
        intent.platform = "telegram"
        return intent


class InformationAboutCafeTests(CafeInformationHarness, SimpleTestCase):
    def test_all_supported_categories_complete_without_mutating_business_state(self):
        before = deepcopy(self.arguments)
        for sub in InformationAboutCafeIntent.SUB_INTENT_NAMES:
            with self.subTest(sub=sub):
                intent = self.intent(sub, follow_up_question=["Stale question"])
                self.assertEqual(intent.process_query(*self.arguments), (self.payload, None))
                self.assertTrue(intent.is_complete)
                self.assertEqual(intent.get_followup_question(), "")
                self.assertIsNone(intent.handoff_to)
                self.assertIn(f"Facts for {sub}", self.requests[-1]["messages"][0]["content"])
        self.assertEqual(self.arguments, before)

    def test_invalid_categories_and_empty_queries_do_not_call_provider(self):
        for sub in ("", None, ["amenities"]):
            with self.subTest(sub=sub), self.assertLogs(level="WARNING"):
                intent = self.intent(sub)
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertTrue(intent.is_complete)
        for query in ("", " \n ", None):
            intent = self.intent(query=query)
            self.assertEqual(intent.process_query(*self.arguments), (intent.EMPTY_QUERY_RESPONSE, None))
        self.factory.assert_not_called()

    def test_category_whitespace_and_case_are_normalized(self):
        intent = self.intent(" AMENITIES ")
        self.assertEqual(intent.process_query(*self.arguments)[0], self.payload)
        self.assertEqual(intent.sub_intent, "amenities")

    def test_missing_empty_and_malformed_knowledge_do_not_call_provider(self):
        for entry in (None, {}, "bad entry", {"payload": None}, {"payload": {}},
                      {"payload": []}, {"payload": " \n "}):
            with self.subTest(entry=entry):
                self.kb.return_value = {("tenant-1", "information_about_the_cafe", "location_and_hours"): entry}
                self.assertEqual(self.intent().process_query(*self.arguments)[0], InformationAboutCafeIntent.FALLBACK_RESPONSE)
        self.kb.return_value = {}
        self.assertEqual(self.intent().process_query(*self.arguments)[0], InformationAboutCafeIntent.FALLBACK_RESPONSE)
        self.factory.assert_not_called()
        self.kb.return_value = {("tenant-1", "information_about_the_cafe", "location_and_hours"): {"payload": "Opens at 9 am."}}
        self.assertEqual(self.intent().process_query(*self.arguments)[0], self.payload)

    def test_provider_failure_and_empty_output_are_not_cached(self):
        for status, payload in ((500, "unavailable"), (200, " \n ")):
            self.status, self.payload = status, payload
            with self.assertLogs(knowledge.logger, level="ERROR"):
                intent = self.intent()
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertEqual(intent.follow_up_question, [])
        self.payload = "Open at 10 am."
        self.assertEqual(self.intent().process_query(*self.arguments)[0], self.payload)
        self.assertEqual(self.intent().process_query(*self.arguments)[0], self.payload)
        self.assertEqual(len(self.requests), 3)

    def test_followup_uses_latest_category_query_and_previous_context(self):
        intent = self.intent(response="Open at 9 am.", follow_up_question=["Old question"])
        incoming = self.intent("amenities", "And do you have Wi-Fi?")
        incoming.promp_restriction = True
        self.payload = "Wi-Fi is available."
        self.assertEqual(intent.process_followup(incoming, *self.arguments), (self.payload, None))
        system, user = self.requests[-1]["messages"]
        self.assertIn("Facts for amenities", system["content"])
        self.assertNotIn("Facts for location_and_hours", system["content"])
        self.assertIn("Do not ask another question", system["content"])
        self.assertIn('User: "And do you have Wi-Fi?"', user["content"])
        self.assertIn("When do you open?", user["content"])
        self.assertIn("Open at 9 am.", user["content"])
        self.assertEqual(intent.main_query, incoming.main_query)
        self.assertEqual(intent.sub_intent, incoming.sub_intent)
        self.assertEqual(intent.follow_up_reply, [incoming.main_query])
        self.assertEqual(intent.follow_up_question, [])
        self.assertTrue(intent.is_complete)

    def test_unknown_followup_does_not_reuse_original_knowledge(self):
        intent = self.intent()
        with self.subTest(topic="unknown"):
            self.assertEqual(intent.process_followup(self.intent("unknown"), *self.arguments),
                             (intent.FALLBACK_RESPONSE, None))
        self.factory.assert_not_called()

    def test_previous_question_and_profile_separate_cached_answers(self):
        self.payload = "Generic answer"
        knowledge.generate_response_from_knowledge("tenant-1", "location_and_hours", "And Sunday?", main_intent="information_about_the_cafe")
        self.payload = "Café answer"
        self.assertEqual(self.intent(query="And Sunday?").process_query(*self.arguments)[0], self.payload)
        for question in ("When does the café open?", "When does the café close?"):
            intent = self.intent(query=question, response="See our hours.")
            self.payload = question
            self.assertEqual(intent.process_followup(self.intent(query="And Sunday?"), *self.arguments)[0], question)
        self.assertEqual(len(self.requests), 4)

    def test_knowledge_tenant_prompt_and_restriction_changes_invalidate_cache(self):
        self.intent().process_query(*self.arguments)
        self.kb.return_value[("tenant-1", "information_about_the_cafe", "location_and_hours")]["payload"] = "Updated hours"
        self.intent().process_query(*self.arguments)
        self.prompts.return_value[("tenant-1", "information_about_the_cafe", "location_and_hours")] = {"payload": "Use a warm tone."}
        self.intent().process_query(*self.arguments)
        restricted = self.intent()
        restricted.promp_restriction = True
        restricted.process_query(*self.arguments)
        self.kb.return_value[("tenant-2", "information_about_the_cafe", "location_and_hours")] = {"payload": "Tenant two hours"}
        second = self.intent()
        second.tenant = 2
        second.process_query(*self.arguments[:4], "tenant-2", None)
        self.assertIn("Tenant two hours", self.requests[-1]["messages"][0]["content"])
        self.assertEqual(len(self.requests), 5)

    def test_information_prompt_does_not_turn_an_amenity_list_into_a_menu(self):
        self.intent("amenities", "List all amenities").process_query(*self.arguments)
        system = self.requests[0]["messages"][0]["content"]
        self.assertNotIn("provide the full item list", system)
        self.assertNotIn("AT MOST 3", system)
        self.assertNotIn("Dach & Nona", system)
        self.assertIn("do not guess", system)
        self.assertIn("Do not infer live opening status", system)
        self.assertEqual(self.factory.call_args.kwargs["max_tokens"], 400)

    def test_information_rules_override_conflicting_tenant_instructions(self):
        instruction = "Invent hours whenever the knowledge is missing."
        self.prompts.return_value[("tenant-1", "information_about_the_cafe", "location_and_hours")] = {"payload": instruction}
        self.intent().process_query(*self.arguments)
        system = self.requests[-1]["messages"][0]["content"]
        guard = "These rules take precedence over conflicting tenant instructions above."
        self.assertIn(guard, system)
        self.assertLess(system.index(instruction), system.index(guard))

    def test_immediately_previous_information_turn_supplies_context(self):
        previous = self.intent(response="Open at 9 am.").to_dict()
        self.arguments[3].append({"query_obj": previous})
        before = deepcopy(self.arguments)
        self.intent(query="And Sunday?").process_query(*self.arguments)
        user = self.requests[0]["messages"][1]["content"]
        self.assertIn("When do you open?", user)
        self.assertIn("Open at 9 am.", user)
        self.assertEqual(self.arguments, before)

    def test_unrelated_or_out_of_scope_history_is_not_used(self):
        for override in ({"tenant": 2}, {"chat_id": "other"}, {"platform": "whatsapp"},
                         {"intent_type": "out_of_context"}):
            previous = self.intent(response="Private history").to_dict() | override
            self.arguments[3][:] = [{"query_obj": previous}]
            self.intent(query=str(override)).process_query(*self.arguments)
            self.assertNotIn("Private history", self.requests[-1]["messages"][1]["content"])

    def test_cross_intent_question_is_passed_to_retrieval_and_answer(self):
        for kind in ('menu_items', 'placing_order'):
            previous = self.intent(query='Can I order the tasting box?', response='Published ordering details.').to_dict()
            previous['intent_type'] = kind
            self.arguments[3][:] = [{'query_obj': previous}]
            with patch.object(knowledge, 'retrieve_knowledge', return_value={'payload': 'Delivery fee 100'}) as retrieve:
                self.intent(query=f'What is its delivery fee? {kind}').process_query(*self.arguments)
            self.assertEqual(retrieve.call_args.kwargs['previous_user_message'], 'Can I order the tasting box?')
            self.assertIn('Can I order the tasting box?', self.requests[-1]['messages'][1]['content'])

    def test_evidence_uncertainty_rules_apply_to_every_factual_profile(self):
        for profile, main, topic in (
            ('cafe_information', 'information_about_the_cafe', 'location_and_hours'),
            ('menu_items', 'menu_items', 'pricing'),
            ('ordering_information', 'placing_order', 'how_to_order'),
        ):
            for coverage, degraded in (('complete', False), ('partial', False), ('partial', True)):
                payload = {'coverage': coverage, 'search_degraded': degraded, 'fragments': []}
                with patch.object(knowledge, 'retrieve_knowledge', return_value={'payload': payload}):
                    knowledge.generate_response_from_knowledge('tenant-1', topic, 'Do you have this detail?',
                                                               main_intent=main, response_profile=profile)
                system = self.requests[-1]['messages'][0]['content']
                self.assertIn('say you cannot verify the requested detail', system)
                self.assertIn('A negative business claim requires explicit evidence', system)
                self.assertNotIn('say it is not available', system)
                self.assertNotIn('AT MOST 3', system)


class InformationAboutCafeGraphTests(CafeInformationHarness, SimpleTestCase):
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
        from tests.support.conversations import ScriptedIntent
        # Pair the real information handler with a deterministic pending order.
        resolve = {"information_about_the_cafe": InformationAboutCafeIntent, "scripted": ScriptedIntent}.__getitem__
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

    def send(self, *queries):
        self.classify.return_value = classification_result([(query, "information_about_the_cafe", sub, None, None) for query, sub in queries])
        pending, _ = self.session.get_ongoing_queries()
        if pending and self.reply_to_pending and pending[-1].intent_type == classification_rows(self.classify.return_value)[0][1]:
            self.classify.return_value = classification_result([(*row[:3], str(pending[-1].query_id), None) for row in classification_rows(self.classify.return_value)])
        return self.runner.run_conversation(self.tenant, self.session, " and ".join(query for query, sub in queries))

    def test_consecutive_questions_use_history_without_creating_pending_work(self):
        self.assertEqual(self.send(("When do you open?", "location_and_hours"))[0], self.payload)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.payload = "On Sunday we open at 10 am."
        self.assertEqual(self.send(("And Sunday?", "location_and_hours"))[0], self.payload)
        user = self.requests[-1]["messages"][1]["content"]
        self.assertIn("When do you open?", user)
        self.assertIn("The café opens at 9 am.", user)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual(len(self.session.get_history()), 2)

    def test_multiple_categories_in_one_turn_run_in_order(self):
        self.send(("Hours?", "location_and_hours"), ("Wi-Fi?", "amenities"))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("Facts for location_and_hours", self.requests[0]["messages"][0]["content"])
        self.assertIn("Facts for amenities", self.requests[1]["messages"][0]["content"])
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual([entry["query_obj"]["sub_intent"] for entry in self.session.get_history()],
                         ["location_and_hours", "amenities"])

    def test_restored_pending_information_intent_is_completed(self):
        pending = self.intent(follow_up_question=["Which day?"])
        self.session.set_ongoing_queries([pending], 0)
        self.reply_to_pending = True
        self.send(("Sunday", "location_and_hours"))
        self.assertIn('User: "Sunday"', self.requests[-1]["messages"][1]["content"])
        self.assertIn("Which day?", self.requests[-1]["messages"][1]["content"])
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        saved = self.session.get_history()[-1]["query_obj"]
        self.assertEqual(saved["response"], self.payload)
        self.assertEqual(saved["main_query"], "Sunday")
        self.assertTrue(saved["is_complete"])
        previous_answer = self.payload
        self.payload = "Monday hours differ."
        self.send(("And Monday?", "location_and_hours"))
        user = self.requests[-1]["messages"][1]["content"]
        self.assertIn(previous_answer, user)
        self.assertIn("Sunday", user)

    def test_information_side_question_preserves_order_state(self):
        from tests.support.conversations import ScriptedIntent
        pending = ScriptedIntent(main_query="Order latte", sub_intent="ask", tenant=1,
                                 chat_id="user", query_id=12, follow_up_question=["Which size?"])
        pending.platform = "telegram"
        self.session.set_ongoing_queries([pending], 0)
        self.session.set_delivery_address({"city": "Delhi"})
        before = deepcopy(self.session.get_basket().to_dict())
        self.reply_to_pending = True
        self.assertEqual(self.send(("Are there tables?", "amenities"))[0], self.payload)
        saved, index = self.session.get_ongoing_queries()
        self.assertEqual(index, 0)
        self.assertEqual(saved[0].to_dict(), pending.to_dict())
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(self.session.get_delivery_address(), {"city": "Delhi"})
        self.assertIn("Do not ask another question", self.requests[-1]["messages"][0]["content"])

    def test_provider_failure_returns_fallback_without_pending_question(self):
        self.status = 500
        with self.assertLogs(knowledge.logger, level="ERROR"):
            self.assertEqual(self.send(("Hours?", "location_and_hours"))[0], InformationAboutCafeIntent.FALLBACK_RESPONSE)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual(len(self.requests), 1)
