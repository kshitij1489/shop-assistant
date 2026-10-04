"""Conversation graph through the real shared chain and an offline HTTP provider."""
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase

from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.session.memory import MemorySessionStore, _session_data
from tests.support.conversations import ScriptedIntent
from tests.support.runtime import install_runtime_fixture
from tests.support.llm import ProviderHarness


class CombinedWorkflowTests(ProviderHarness, SimpleTestCase):
    def setUp(self):
        super().setUp()
        install_runtime_fixture(self)
        self.tenant = SimpleNamespace(id=1, pk=1, api_key="tenant-1")
        self.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")
        self.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")
        self.enterContext(patch.object(self.runner, "get_chat_ongoing_session", return_value=object()))
        self.enterContext(patch.object(self.runner, "prepare_order_session", side_effect=lambda state, anchor, *a: (anchor, None)))
        self.enterContext(patch.object(self.runner, "recover_checkout"))
        self.enterContext(patch.object(self.runner, "sync_checkout_basket"))
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        for module in (base, self.graph):
            self.enterContext(patch.object(module, "get_intent", return_value=ScriptedIntent))
        from commerce.policy import Policy, evaluation_policy
        self.enterContext(patch("chatbot_core.logic.cafe.ordering_limits.load_policy",
                                return_value=Policy.model_validate(evaluation_policy())))
        self.enterContext(patch("chatbot_core.logic.cafe.basket.search_cache", return_value={
            "item_id": "latte", "item_variant_id": "regular", "unit_price": 100,
        }))
        self.enterContext(patch("chatbot_core.logic.cafe.catalog.load_catalog", return_value={
            "latte": {"item_id": "latte", "name": "latte", "modifier_groups": [],
                      "variants": [{"id": "regular", "name": "regular", "price": "100"}]},
        }))

    def store(self, suffix):
        store = MemorySessionStore("combined-" + suffix, tenant_id="1", platform="telegram")
        self.addCleanup(_session_data.pop, store._storage_id, None)
        return store

    def output(self, *pairs):
        self.payload = {"declared_constraints": [], "classifications": [
            {"query": query, "rephrased_sentence": query, "intent": "scripted", "sub_intent": sub, "reply_to": None, "clarification": None} for query, sub in pairs
        ]}

    def test_graph_calls_model_once_and_executes_multiple_units_in_order(self):
        cache.clear()
        ScriptedIntent.operations = []
        store = self.store(self.runner.__name__)
        self.output(("add", "add"), ("update", "update"))
        count = len(self.requests)
        reply, basket = self.runner.run_conversation(self.tenant, store, "add then update")
        self.assertEqual(len(self.requests) - count, 1)
        self.assertEqual(ScriptedIntent.operations, ["add", "update"])
        self.assertEqual(reply, "add reply. update reply")
        self.assertEqual(basket[0]["quantity"], 3)

    def test_pending_answer_remains_separate_from_new_action(self):
        cache.clear()
        ScriptedIntent.operations = []
        store = self.store(self.runner.__name__)
        self.output(("ask", "ask"))
        self.runner.run_conversation(self.tenant, store, "ask")
        self.output(("large", "large"), ("add", "add"))
        self.payload["classifications"][0]["reply_to"] = str(store.get_ongoing_queries()[0][-1].query_id)
        count = len(self.requests)
        self.runner.run_conversation(self.tenant, store, "large, then add")
        self.assertEqual(len(self.requests) - count, 1)
        self.assertEqual(ScriptedIntent.operations, ["ask", "followup:large", "add"])
        payload = json.loads(self.requests[-1]["messages"][1]["content"])
        self.assertEqual(payload['new_user_message'], 'large, then add')
        self.assertEqual(payload['conversation_context']['last_assistant_question'], 'Which size?')
        self.assertEqual(store.get_ongoing_queries(), ([], None))

    def test_english_rewrites_and_original_evidence_survive_split_followup_and_history(self):
        store = self.store('rewrites')
        self.output(('ask', 'ask'))
        self.payload['response_language'] = 'hi-Latn'
        self.payload['classifications'][0]['rephrased_sentence'] = 'Choose a size for the pending latte'
        with patch.object(self.graph, 'localize_reply', side_effect=lambda reply, question, lang: (reply, question)):
            self.runner.run_conversation(self.tenant, store, 'latte chahiye')
            pending = store.get_ongoing_queries()[0][-1]
            self.assertEqual(pending.rephrased_sentence, 'Choose a size for the pending latte')
            self.assertEqual(pending.original_query, 'latte chahiye')
            self.output(('large', 'large'), ('add', 'add'))
            self.payload['classifications'][0].update(
                reply_to=str(pending.query_id), rephrased_sentence='Use the large size for the pending latte')
            self.payload['classifications'][1]['rephrased_sentence'] = 'Add another latte'
            self.runner.run_conversation(self.tenant, store, 'bada, aur ek latte add karo')
        rows = [entry['query_obj'] for entry in store.get_history()[-2:]]
        self.assertEqual([row['rephrased_sentence'] for row in rows], [
            'Use the large size for the pending latte', 'Add another latte'])
        self.assertTrue(all(row['original_query'] == 'bada, aur ek latte add karo' for row in rows))
        self.assertTrue(all(row['response_language'] == 'hi-Latn' for row in rows))

    def test_invalid_second_row_preserves_pending_basket_address_history(self):
        for failure in ("invalid", "provider"):
            with self.subTest(failure=failure):
                cache.clear()
                self.status = 200
                ScriptedIntent.operations = []
                store = self.store(self.runner.__name__ + failure)
                self.output(("save", "save"), ("ask", "ask"))
                self.runner.run_conversation(self.tenant, store, "save then ask")
                before = deepcopy(store.read_snapshot())
                self.output(("add", "add"), ("large", "invented"))
                self.payload['declared_constraints'] = ['I have a milk allergy.']
                if failure == "provider":
                    self.status = 500
                with self.assertLogs("chatbot_core.logic.cafe.prompts.normalize_and_classify", "ERROR"):
                    reply, basket = self.runner.run_conversation(self.tenant, store, "large then add")
                self.assertIn("ask again", reply)
                self.assertIsNone(basket)
                after = store.read_snapshot()
                for key in ("basket", "delivery_address", "checklist", "ongoing_query_queue",
                            "awaiting_followup_index", "chat_history"):
                    self.assertEqual(after[key], before[key])
                self.assertEqual(ScriptedIntent.operations, ["save", "ask"])

    def test_missing_control_documents_route_to_clarification(self):
        combined = importlib.import_module("chatbot_core.logic.cafe.prompts.normalize_and_classify")
        self.payload = {"declared_constraints": [], "classifications": [{
            "query": "hmm", "rephrased_sentence": "An unclear acknowledgement", "intent": "insufficient_information", "sub_intent": "insufficient_information", "reply_to": None, "clarification": None,
        }]}
        with patch.object(combined, "get_intent_classification_cache", return_value={}):
            cache.clear()
            count = len(self.requests)
            response, _ = self.runner.run_conversation(self.tenant, self.store(self.runner.__name__), "hmm")
            self.assertEqual(response, "insufficient_information reply")
            self.assertEqual(len(self.requests) - count, 1)
