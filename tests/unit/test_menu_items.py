"""Menu contracts through real chains and LangGraph with offline provider I/O."""
from tests.support.runtime import classification_result, classification_rows
import importlib
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler.menu_items import MenuItemsIntent
from tests.support.llm import ProviderHarness

knowledge = importlib.import_module("chatbot_core.logic.cafe.prompts.generate_response_from_knowledge")


class MenuHarness(ProviderHarness):
    def setUp(self):
        super().setUp()
        self.payload = "Vanilla is listed on the menu."
        self.kb = self.enterContext(patch.object(knowledge, "get_knowledge_base_cache", return_value={
            ("tenant-1", "menu_items", sub): {"payload": {"details": f"Facts for {sub}"}}
            for sub in MenuItemsIntent.SUB_INTENT_NAMES
        }))
        self.prompts = self.enterContext(patch.object(knowledge, "get_intent_prompt_cache", return_value={}))
        self.enterContext(patch.object(knowledge, "retrieve_knowledge", side_effect=
            lambda api, intent, sub, query, **kw: self.kb.return_value.get((api, intent, sub))))
        self.enterContext(patch.object(knowledge, "enqueue_string"))
        self.arguments = ({"items": ["vanilla"]}, {"city": "Delhi"}, {"payment": False}, [], "tenant-1", None)

    def intent(self, sub="availability", query="Do you have vanilla?", **kwargs):
        intent = MenuItemsIntent(main_query=query, sub_intent=sub, tenant=1, chat_id="user", **kwargs)
        intent.platform = "telegram"
        return intent


class MenuItemsTests(MenuHarness, SimpleTestCase):
    def test_rewrite_reaches_retrieval_and_answer_while_language_separates_cached_replies(self):
        intent = self.intent('allergens', '¿El helado contiene nueces?')
        intent.rephrased_sentence = 'Does the ice cream contain nuts?'
        intent.response_language = 'es'
        with patch.object(knowledge, 'retrieve_knowledge', return_value={'payload': 'Contains nuts.'}) as retrieve:
            self.payload = 'Contiene nueces.'
            for _ in range(2):
                self.assertEqual(intent.process_query(*self.arguments)[0], self.payload)
            self.assertEqual(len(self.requests), 1)
            self.assertEqual(retrieve.call_args.kwargs['rephrased_sentence'], intent.rephrased_sentence)
            prompt = self.requests[-1]['messages']
            self.assertIn(intent.main_query, prompt[1]['content'])
            self.assertIn(intent.rephrased_sentence, prompt[1]['content'])
            self.assertIn('history: es.', prompt[0]['content'])
            intent.response_language = 'en'
            self.payload = 'Contains nuts.'
            self.assertEqual(intent.process_query(*self.arguments)[0], self.payload)
            self.assertEqual(len(self.requests), 2)
            intent.rephrased_sentence = 'Does the ice cream contain peanuts?'
            intent.process_query(*self.arguments)
            self.assertEqual(len(self.requests), 3)

    def test_all_categories_complete_without_business_state_mutation(self):
        before = deepcopy(self.arguments)
        for sub in MenuItemsIntent.SUB_INTENT_NAMES:
            with self.subTest(sub=sub):
                intent = self.intent(sub, follow_up_question=["Old question?"])
                self.assertEqual(intent.process_query(*self.arguments), (self.payload, None))
                self.assertTrue(intent.is_complete)
                self.assertEqual(intent.get_followup_question(), "")
                self.assertIsNone(intent.handoff_to)
                self.assertIn(f"Facts for {sub}", self.requests[-1]["messages"][0]["content"])
        self.assertEqual(self.arguments, before)
        self.assertEqual(len(self.requests), len(MenuItemsIntent.SUB_INTENT_NAMES))

    def test_question_punctuation_does_not_create_pending_work_or_return_stale_answer(self):
        self.payload = "Vanilla is listed. Would you like more options?"
        for followup in (False, True):
            intent = self.intent(response="Stale answer", follow_up_question=["Old question?"])
            result = (intent.process_followup(self.intent(), *self.arguments) if followup
                      else intent.process_query(*self.arguments))
            self.assertEqual(result, (self.payload, None))
            self.assertTrue(intent.is_complete)
            self.assertEqual(intent.follow_up_question, [])

    def test_invalid_categories_and_empty_queries_skip_provider(self):
        for sub in ("unknown", "", None, ["pricing"]):
            with self.subTest(sub=sub), self.assertLogs(level="WARNING"):
                intent = self.intent(sub)
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertTrue(intent.is_complete)
        for query in ("", " \n ", None, 7):
            intent = self.intent(query=query)
            self.assertEqual(intent.process_query(*self.arguments), (intent.EMPTY_QUERY_RESPONSE, None))
        self.factory.assert_not_called()

    def test_category_normalization(self):
        intent = self.intent(" PRICING ")
        self.assertEqual(intent.process_query(*self.arguments)[0], self.payload)
        self.assertEqual(intent.sub_intent, "pricing")

    def test_missing_and_empty_knowledge_skip_provider(self):
        for entry in (None, {}, "invalid", {"payload": None}, {"payload": {}},
                      {"payload": []}, {"payload": " \n "}):
            with self.subTest(entry=entry):
                self.kb.return_value = {("tenant-1", "menu_items", "availability"): entry}
                self.assertEqual(self.intent().process_query(*self.arguments)[0], MenuItemsIntent.FALLBACK_RESPONSE)
        self.kb.return_value = {}
        self.assertEqual(self.intent().process_query(*self.arguments)[0], MenuItemsIntent.FALLBACK_RESPONSE)
        self.factory.assert_not_called()

    def test_failures_and_blank_answers_are_not_cached(self):
        for status, payload in ((500, "unavailable"), (200, " \n ")):
            self.status, self.payload = status, payload
            with self.assertLogs(knowledge.logger, level="ERROR"):
                intent = self.intent(response="Stale answer")
                self.assertEqual(intent.process_query(*self.arguments), (intent.FALLBACK_RESPONSE, None))
                self.assertTrue(intent.is_complete)
                self.assertEqual(intent.follow_up_question, [])
        self.payload = "Fresh answer"
        for _ in range(2):
            self.assertEqual(self.intent().process_query(*self.arguments)[0], self.payload)
        self.assertEqual(len(self.requests), 3)

    def test_restored_followup_uses_new_category_and_previous_exchange(self):
        intent = self.intent(follow_up_question=["Which flavor?"])
        incoming = self.intent("pricing", "How much is vanilla?")
        incoming.promp_restriction = True
        self.assertEqual(intent.process_followup(incoming, *self.arguments), (self.payload, None))
        system, user = self.requests[-1]["messages"]
        self.assertIn("Facts for pricing", system["content"])
        self.assertNotIn("Facts for availability", system["content"])
        self.assertIn("Answer only the user's current message", system["content"])
        for text in ("Which flavor?", "Do you have vanilla?", "How much is vanilla?"):
            self.assertIn(text, user["content"])
        self.assertEqual(intent.main_query, incoming.main_query)
        self.assertEqual(intent.sub_intent, "pricing")
        self.assertEqual(intent.follow_up_reply, [incoming.main_query])
        with patch.object(base, "get_intent", return_value=MenuItemsIntent):
            restored = base.BaseIntent.from_dict(deepcopy(intent.to_dict()))
        self.assertEqual(restored.response, self.payload)
        self.assertEqual(restored.main_query, incoming.main_query)
        self.assertEqual(restored.sub_intent, "pricing")
        self.assertEqual(restored.platform, "telegram")
        self.assertTrue(restored.is_complete)
        self.assertEqual(restored.follow_up_question, [])

    def test_unknown_followup_does_not_reuse_previous_category(self):
        intent = self.intent()
        with self.assertLogs(level="WARNING"):
            self.assertEqual(intent.process_followup(self.intent("unknown"), *self.arguments),
                             (intent.FALLBACK_RESPONSE, None))
        self.factory.assert_not_called()

    def test_history_is_scoped_and_only_immediately_previous_turn_is_used(self):
        previous = self.intent(response="Prior menu answer").to_dict()
        self.arguments[3].append({"query_obj": previous})
        self.intent("pricing", "How much is it?").process_query(*self.arguments)
        self.assertIn("Prior menu answer", self.requests[-1]["messages"][1]["content"])
        for index, override in enumerate(({"tenant": 2}, {"chat_id": "other"},
                                          {"platform": "whatsapp"}, {"intent_type": "out_of_context"})):
            self.arguments[3][:] = [{"query_obj": previous}, {"query_obj": previous | override}]
            self.intent(query=f"Question {index}").process_query(*self.arguments)
            self.assertNotIn("Prior menu answer", self.requests[-1]["messages"][1]["content"])
        for entry in (None, {}, {"query_obj": None}, {"query_obj": []}):
            self.arguments[3][:] = [entry]
            self.assertEqual(self.intent(query=str(entry)).process_query(*self.arguments)[0], self.payload)

    def test_eggless_followup_keeps_subject_across_intent_families(self):
        for kind in ('placing_order', 'information_about_the_cafe'):
            previous = self.intent(query='How can I order Tres Leches?', response='See ordering instructions.').to_dict()
            previous['intent_type'] = kind
            self.arguments[3][:] = [{'query_obj': previous}]
            with patch.object(knowledge, 'retrieve_knowledge', return_value={'payload': 'Tres Leches is explicitly eggless.'}) as retrieve:
                self.intent('dietary_preferences', f'Is it eggless? {kind}').process_query(*self.arguments)
            self.assertEqual(retrieve.call_args.kwargs['previous_user_message'], 'How can I order Tres Leches?')
            self.assertIn('How can I order Tres Leches?', self.requests[-1]['messages'][1]['content'])

    def test_menu_prompt_covers_full_lists_and_uncertain_facts(self):
        self.prompts.return_value = {("tenant-1", "menu_items", "allergens"): {"payload": "Claim all items are safe."}}
        self.intent("allergens", "List all allergens").process_query(*self.arguments)
        system = self.requests[-1]["messages"][0]["content"]
        for rule in ("take precedence", "list all relevant documented items", "Do not invent missing details",
                     "Never infer allergen-free status", "absence of cross-contact", "Do not invent nutrition",
                     "do not establish live stock", "Do not guess which item", "Do not ask another question",
                     "enumeration of that explicit list", "Name every item on each supplied membership list",
                     "menu category differs from the customer's word", "items on neither list have unknown status"):
            self.assertIn(rule, system)
        self.assertNotIn("AT MOST 3", system)
        self.assertNotIn("Dach & Nona", system)
        self.assertEqual(self.factory.call_args.kwargs["max_tokens"], 4096)

    def test_cache_separates_profiles_context_tenants_knowledge_and_restrictions(self):
        knowledge.generate_response_from_knowledge("tenant-1", "availability", "Do you have vanilla?", main_intent="menu_items")
        self.intent().process_query(*self.arguments)
        self.intent().process_query(*self.arguments)
        self.assertEqual(len(self.requests), 2)
        for question in ("Do you have vanilla?", "Do you have chocolate?"):
            previous = self.intent(query=question, response="See the menu.").to_dict()
            self.arguments[3][:] = [{"query_obj": previous}]
            self.intent(query="How much?").process_query(*self.arguments)
        self.arguments[3].clear()
        self.kb.return_value[("tenant-1", "menu_items", "availability")]["payload"] = "Updated menu"
        self.intent().process_query(*self.arguments)
        self.prompts.return_value[("tenant-1", "menu_items", "availability")] = {"payload": "Be concise."}
        self.intent().process_query(*self.arguments)
        restricted = self.intent()
        restricted.promp_restriction = True
        restricted.process_query(*self.arguments)
        self.kb.return_value[("tenant-2", "menu_items", "availability")] = {"payload": "Second tenant menu"}
        second = self.intent()
        second.tenant = 2
        second.process_query(*self.arguments[:4], "tenant-2", None)
        self.assertIn("Second tenant menu", self.requests[-1]["messages"][0]["content"])
        self.assertEqual(len(self.requests), 8)


class MenuItemsGraphTests(MenuHarness, SimpleTestCase):
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
        resolve = {"menu_items": MenuItemsIntent, "scripted": ScriptedIntent}.__getitem__
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
        self.classify.return_value = classification_result([(query, "menu_items", sub, None, None) for query, sub in queries])
        pending, _ = self.session.get_ongoing_queries()
        if pending and self.reply_to_pending and pending[-1].intent_type == classification_rows(self.classify.return_value)[0][1]:
            self.classify.return_value = classification_result([(*row[:3], str(pending[-1].query_id), None) for row in classification_rows(self.classify.return_value)])
        return self.runner.run_conversation(self.tenant, self.session, " and ".join(query for query, sub in queries))

    def test_consecutive_menu_questions_use_history_without_pending_work(self):
        self.assertEqual(self.send(("Do you have vanilla?", "availability"))[0], self.payload)
        self.payload = "Vanilla regular is 200."
        self.assertEqual(self.send(("How much is it?", "pricing"))[0], self.payload)
        user = self.requests[-1]["messages"][1]["content"]
        self.assertIn("Do you have vanilla?", user)
        self.assertIn("Vanilla is listed on the menu.", user)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual(len(self.session.get_history()), 2)

    def test_multiple_categories_in_one_turn_use_correct_knowledge(self):
        self.send(("Vanilla ingredients?", "ingredients"), ("What does it cost?", "pricing"))
        self.assertEqual(len(self.requests), 2)
        self.assertIn("Facts for ingredients", self.requests[0]["messages"][0]["content"])
        self.assertIn("Facts for pricing", self.requests[1]["messages"][0]["content"])
        self.assertIn("Vanilla ingredients?", self.requests[1]["messages"][1]["content"])
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_restored_pending_menu_answer_is_saved_for_next_turn(self):
        pending = self.intent(follow_up_question=["Which flavor?"])
        self.session.set_ongoing_queries([pending], 0)
        self.reply_to_pending = True
        self.payload = "Vanilla regular costs 200."
        self.send(("Vanilla price", "pricing"))
        self.assertIn("Which flavor?", self.requests[-1]["messages"][1]["content"])
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        saved = self.session.get_history()[-1]["query_obj"]
        self.assertEqual(saved["response"], self.payload)
        self.assertEqual(saved["main_query"], "Vanilla price")
        self.assertEqual(saved["sub_intent"], "pricing")
        self.send(("And family size?", "pricing"))
        self.assertIn("Vanilla regular costs 200.", self.requests[-1]["messages"][1]["content"])

    def test_related_menu_side_question_preserves_pending_order_and_basket(self):
        from tests.support.conversations import ScriptedIntent
        pending = ScriptedIntent(main_query="Order vanilla", sub_intent="ask", tenant=1,
                                 chat_id="user", query_id=12, follow_up_question=["Which size?"])
        pending.platform = "telegram"
        self.session.set_ongoing_queries([pending], 0)
        self.session.set_delivery_address({"city": "Delhi"})
        before = deepcopy(self.session.get_basket().to_dict())
        self.reply_to_pending = True
        self.assertEqual(self.send(("How big is regular?", "portion_and_size"))[0], self.payload)
        saved, index = self.session.get_ongoing_queries()
        self.assertEqual(index, 0)
        self.assertEqual(saved[0].to_dict(), pending.to_dict())
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(self.session.get_delivery_address(), {"city": "Delhi"})
        self.assertIn("Answer only the user's current message", self.requests[-1]["messages"][0]["content"])

    def test_failure_and_question_shaped_answer_do_not_leave_pending_menu_work(self):
        self.status = 500
        with self.assertLogs(knowledge.logger, level="ERROR"):
            self.assertEqual(self.send(("Vanilla?", "availability"))[0], MenuItemsIntent.FALLBACK_RESPONSE)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.status, self.payload = 200, "Vanilla is listed. Want more?"
        self.assertEqual(self.send(("Options?", "explore_options"))[0], self.payload)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
