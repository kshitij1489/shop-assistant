"""Base intent state boundaries and production graph/session integration."""
from tests.support.runtime import classification_result
from copy import deepcopy
import importlib
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from chatbot_core import capabilities
from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler.general import GeneralIntent


class BaseIntentTests(SimpleTestCase):
    def intent(self, **overrides):
        fields = dict(main_query="order latte", sub_intent="greeting", tenant=1,
                      chat_id="user", query_id=12)
        fields.update(overrides)
        obj = GeneralIntent(**fields)
        obj.platform = "telegram"
        return obj

    def test_registry_normalizes_names_and_loads_only_requested_module(self):
        with patch.object(capabilities, "import_module", return_value=SimpleNamespace(GeneralIntent=GeneralIntent)) as load:
            self.assertIs(base.get_intent(" General "), GeneralIntent)
        load.assert_called_once_with("chatbot_core.logic.cafe.intent_handler.general")

    def test_registry_rejects_invalid_names_before_importing_handlers(self):
        with patch.object(capabilities, "import_module") as load:
            for name in (None, "", "  ", 3, [], {}, "unknown"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    base.get_intent(name)
        load.assert_not_called()

    def test_all_handlers_own_nested_state_and_accept_null_collections(self):
        for name in capabilities.CAPABILITIES:
            with self.subTest(handler=name):
                handler = base.get_intent(name)
                fields = dict(main_query="pending", sub_intent="pending", tenant=1, chat_id="user")
                basket = {"pending": [{"name": "latte"}]}
                questions, replies = ["Which size?"], ["large"]
                obj = handler(**fields, basket_item=basket,
                              follow_up_question=questions, follow_up_reply=replies)
                obj.basket_item["pending"][0]["name"] = "tea"
                obj.follow_up_question.clear()
                obj.follow_up_reply.clear()
                self.assertEqual(basket, {"pending": [{"name": "latte"}]})
                self.assertEqual((questions, replies), (["Which size?"], ["large"]))
                empty = handler(**fields, basket_item=None, follow_up_question=None, follow_up_reply=None)
                self.assertEqual((empty.basket_item, empty.follow_up_question, empty.follow_up_reply), ({}, [], []))
                empty.basket_item["new"] = True
                empty.follow_up_question.append("Question")
                empty.follow_up_reply.append("Reply")
                other = handler(**fields)
                self.assertEqual((other.basket_item, other.follow_up_question, other.follow_up_reply), ({}, [], []))

    def test_serialization_is_a_snapshot_in_both_directions(self):
        obj = self.intent(basket_item={"items": [{"quantity": 1}]},
                          follow_up_question=["Which size?"], follow_up_reply=["large"])
        obj.delivery_address = {"coordinates": {"latitude": 28}}
        saved = obj.to_dict()
        original = deepcopy(saved)
        obj.basket_item["items"][0]["quantity"] = 2
        obj.delivery_address["coordinates"]["latitude"] = 30
        obj.follow_up_question.clear()
        obj.follow_up_reply.clear()
        self.assertEqual(saved, original)
        restored = base.BaseIntent.from_dict(saved)
        restored.basket_item["items"][0]["quantity"] = 3
        restored.delivery_address["coordinates"]["latitude"] = 32
        restored.follow_up_question.clear()
        restored.follow_up_reply.clear()
        self.assertEqual(saved, original)

    def test_json_roundtrip_preserves_state_without_inventing_basket_fields(self):
        for query_id in (12, "old-query-id"):
            obj = self.intent(query_id=query_id, is_complete=True, ignored_count=2,
                              response="Done", basket_item={"custom": [1]},
                              follow_up_question=["question"], follow_up_reply=["reply"])
            obj.delivery_address = {"city": "Delhi"}
            obj.original_query = 'haan'
            obj.rephrased_sentence = 'Confirm the Home delivery address'
            obj.response_language = 'hi-Latn'
            saved = json.loads(json.dumps(obj.to_dict()))
            restored = base.BaseIntent.from_dict(saved)
            self.assertEqual(restored.to_dict(), saved)
            self.assertIsInstance(restored, GeneralIntent)

    def test_optional_saved_fields_and_old_empty_address_default(self):
        fields = dict(main_query="hi", sub_intent="greeting", tenant=1, chat_id="user",
                      intent_type=" GENERAL ")
        before = deepcopy(fields)
        obj = base.BaseIntent.from_dict(fields)
        self.assertEqual(fields, before)
        self.assertEqual(obj.intent_type, "general")
        self.assertEqual((obj.basket_item, obj.delivery_address), ({}, {}))
        for address in (None, []):
            obj = base.BaseIntent.from_dict({**fields, "basket_item": None,
                                            "follow_up_question": None, "follow_up_reply": None,
                                            "delivery_address": address})
            self.assertEqual((obj.follow_up_question, obj.follow_up_reply, obj.delivery_address), ([], [], {}))

    def test_malformed_saved_state_is_rejected_without_modification(self):
        for field, value in (
            ("basket_item", []), ("basket_item", "latte"),
            ("follow_up_question", "Which size?"), ("follow_up_reply", [None]),
            ("delivery_address", ["Delhi"]), ("is_complete", "false"),
            ("ignored_count", -1), ("ignored_count", True), ("ignored_count", "0"),
        ):
            saved = {**self.intent().to_dict(), field: value}
            before = deepcopy(saved)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                base.BaseIntent.from_dict(saved)
            self.assertEqual(saved, before)
        for saved in (None, [], {}, {"intent_type": "unknown"}):
            with self.subTest(saved=saved), self.assertRaises(ValueError):
                base.BaseIntent.from_dict(saved)

    def test_transient_routing_is_not_replayed_from_session(self):
        obj = self.intent()
        obj.request_handoff("general", sub_intent="thanks")
        obj.promp_restriction = True
        saved = obj.to_dict()
        self.assertNotIn("handoff_to", saved)
        self.assertNotIn("handoff_overrides", saved)
        self.assertNotIn("promp_restriction", saved)
        # Even extra fields in old payloads cannot replay routing commands.
        saved.update(handoff_to="general", handoff_overrides={"sub_intent": "thanks"}, promp_restriction=True)
        restored = base.BaseIntent.from_dict(saved)
        self.assertIsNone(restored.handoff_to)
        self.assertEqual(restored.handoff_overrides, {})
        self.assertFalse(restored.promp_restriction)

    def test_handoff_resets_task_state_preserves_scope_and_isolates_nested_data(self):
        source = self.intent(response="old answer", is_complete=True, ignored_count=2,
                             basket_item={"items": [{"quantity": 1}]},
                             follow_up_question=["old question"], follow_up_reply=["old reply"])
        source.delivery_address = {"coordinates": {"latitude": 28}}
        source.request_handoff(" GENERAL ")
        before = source.to_dict()
        with patch.object(GeneralIntent, "process_query") as process:
            first = source.build_handoff_intent()
            second = source.build_handoff_intent()
        process.assert_not_called()
        self.assertEqual((first.tenant, first.chat_id, first.query_id, first.platform), (1, "user", 12, "telegram"))
        self.assertEqual((first.sub_intent, first.response, first.is_complete, first.ignored_count),
                         ("general", "", False, 0))
        self.assertEqual((first.follow_up_question, first.follow_up_reply), ([], []))
        self.assertEqual(first.delivery_address, source.delivery_address)
        first.basket_item["items"][0]["quantity"] = 4
        first.delivery_address["coordinates"]["latitude"] = 30
        self.assertEqual(source.to_dict(), before)
        self.assertEqual(second.basket_item["items"][0]["quantity"], 1)
        self.assertIsNone(second.handoff_to)

    def test_handoff_overrides_are_snapshots_and_last_request_wins(self):
        source = self.intent()
        self.assertIsNone(source.build_handoff_intent())
        questions = ["Confirm?"]
        basket = {"items": [{"quantity": 2}]}
        address = {"city": "Delhi"}
        source.request_handoff("general", sub_intent="thanks", follow_up_question=questions,
                               basket_item=basket, delivery_address=address)
        questions.clear()
        basket["items"][0]["quantity"] = 8
        address.clear()
        target = source.build_handoff_intent()
        self.assertEqual((target.sub_intent, target.follow_up_question, target.delivery_address),
                         ("thanks", ["Confirm?"], {"city": "Delhi"}))
        self.assertEqual(target.basket_item["items"][0]["quantity"], 2)
        target.follow_up_question.clear()
        self.assertEqual(source.handoff_overrides["follow_up_question"], ["Confirm?"])
        source.request_handoff("general", sub_intent="goodbye")
        self.assertEqual(source.build_handoff_intent().sub_intent, "goodbye")

    def test_invalid_handoffs_cannot_replace_valid_request_or_override_scope(self):
        obj = self.intent()
        obj.request_handoff("general", sub_intent="thanks")
        for field in ("tenant", "chat_id", "platform", "query_id", "intent_type", "typo"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                obj.request_handoff("general", **{field: "other"})
        for name in (None, "", [], "unknown"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                obj.request_handoff(name)
        self.assertEqual((obj.handoff_to, obj.handoff_overrides), ("general", {"sub_intent": "thanks"}))

    def test_knowledge_answer_keeps_tenant_and_channel_context(self):
        obj = self.intent()
        module = importlib.import_module("chatbot_core.logic.cafe.prompts.answer_from_knowledge")
        answer = self.enterContext(patch.object(module, "answer_from_knowledge", return_value="answer"))
        self.assertEqual(obj.answer_from_knowledge("facts", "question"), "answer")
        answer.assert_called_once_with("facts", "question", tenant_key="1", user_id="user",
                                       platform="telegram", sub_intent="greeting", main_intent="general")


class BaseIntentGraphTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")
        cls.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")

    def setUp(self):
        from chatbot_core.logic.cafe.session import memory
        self.enterContext(patch.object(memory, "_session_data", {}))
        self.session = memory.MemorySessionStore("user", tenant_id=1, platform="telegram")
        self.tenant = SimpleNamespace(id=1, pk=1, api_key="tenant-1")
        self.enterContext(patch.object(self.runner, "get_chat_ongoing_session", return_value=object()))
        from tests.support.runtime import install_runtime_fixture
        install_runtime_fixture(self, synthetic=False)
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.enterContext(patch.object(self.graph, "normalize_and_classify", return_value=classification_result([("hi", "general", "greeting", None, None)])))

    def test_real_registry_handoff_is_queued_by_graph_and_roundtrips(self):
        def business(obj, *args):
            obj.request_handoff("insufficient_information", follow_up_question=["What would you like?"])
            obj.is_complete = True
            return "Hello", None
        with patch.object(GeneralIntent, "process_query", business):
            response, _ = self.runner.run_conversation(self.tenant, self.session, "hi")
        self.assertEqual(response, "Hello What would you like?")
        pending, index = self.session.get_ongoing_queries()
        self.assertEqual((len(pending), index), (1, 0))
        obj = pending[0]
        self.assertEqual((obj.intent_type, obj.tenant, obj.chat_id, obj.platform),
                         ("insufficient_information", 1, "user", "telegram"))
        self.assertIsNone(obj.handoff_to)
        self.assertEqual(obj.delivery_address, {})
        self.assertEqual(self.session.get_history()[0]["query_obj"]["intent_type"], "general")

    def test_all_registered_handlers_restore_through_shared_contract(self):
        for name in capabilities.CAPABILITIES:
            with self.subTest(name=name):
                handler = base.get_intent(name)
                obj = handler(main_query="pending", sub_intent="pending", tenant=1,
                              chat_id="user", query_id=7, ignored_count=1,
                              basket_item={"pending": [{"name": "latte"}]},
                              follow_up_question=["Question?"], follow_up_reply=["Reply"])
                obj.platform = "telegram"
                obj.delivery_address = {"city": "Delhi"}
                snapshot = json.loads(json.dumps(obj.to_dict()))
                restored = base.BaseIntent.from_dict(snapshot)
                self.assertIsInstance(restored, handler)
                self.assertEqual(restored.to_dict(), snapshot)

    def test_failed_graph_load_does_not_mutate_persisted_pending_state(self):
        obj = GeneralIntent(main_query="hello", sub_intent="greeting", tenant=1,
                            chat_id="user", basket_item={"items": [{"quantity": 1}]},
                            follow_up_question=["Question?"])
        self.session.set_ongoing_queries([obj], 0)
        before = deepcopy(self.session._store())
        with patch.object(GeneralIntent, "process_query", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                self.runner.run_conversation(self.tenant, self.session, "hi")
        self.assertEqual(self.session._store()["ongoing_query_queue"], before["ongoing_query_queue"])
        # Saving/loading memory sessions is independent of caller mutations too.
        obj.follow_up_question.clear()
        obj.basket_item["items"][0]["quantity"] = 5
        loaded = self.session.get_ongoing_queries()[0][0]
        loaded.follow_up_question.clear()
        loaded.basket_item["items"][0]["quantity"] = 8
        self.assertEqual(self.session._store()["ongoing_query_queue"], before["ongoing_query_queue"])
