"""Conversation contracts for the existing handler, without provider/network I/O."""
from tests.support.runtime import classification_result, classification_rows
import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch, ANY, MagicMock

from django.test import SimpleTestCase, TestCase

from chatbot_core.logic.cafe.intent_handler import base as intent_base
from chatbot_core.logic.cafe.intent_handler.general import GeneralIntent
from chatbot_core.logic.cafe.session import redis_session, memory
from chatbot_core.logic.cafe.session.django import DjangoSessionStore
from chatbot_core.scope import session_identity
from chatbot_core.chat_session import get_chat_ongoing_session, update_chat_session_order
from chatbot_core.models import TenantInfo, TenantJSONDoc
from chatbot_core import knowledge_cache
from orders.models import ChatSession, Customer, Order


from tests.support.conversations import FakeRedis, ScriptedIntent


class ConversationContract:
    """Behavioral assertions for the conversation graph and session persistence."""
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch('chatbot_core.vector_store.embedding_client.get_embedding',
                                   side_effect=AssertionError('Unexpected embedding request')))
        cls.handler_module = importlib.import_module("chatbot_core.logic.tenant_handlers.cafe_handler")
        cls.operations = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")

    def setUp(self):
        self.enterContext(patch.object(self.handler_module.logger, "exception"))
        self.redis = FakeRedis()
        self.enterContext(patch.object(redis_session, "_redis", self.redis))
        self.enterContext(patch.object(intent_base, "get_intent", return_value=ScriptedIntent))
        self.enterContext(patch.object(self.operations, "get_intent", return_value=ScriptedIntent))
        self.enterContext(patch.object(self.operations, "enqueue_string"))
        from tests.support.runtime import install_runtime_fixture
        install_runtime_fixture(self)
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.lookup = self.enterContext(patch.object(self.runner, "get_chat_ongoing_session", return_value=object()))
        self.create = self.enterContext(patch.object(self.runner, "create_new_chat_session"))
        self.classify = self.enterContext(patch.object(self.operations, "normalize_and_classify"))
        self.reply_to_pending = False
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
        ScriptedIntent.operations = []
        self.tenant = SimpleNamespace(id=1, pk=1, api_key="tenant-1")

    def store(self, tenant=None, platform="telegram"):
        return redis_session.RedisSessionStore("same-user", tenant_id=(tenant or self.tenant).id, platform=platform)

    def persisted_summary(self):
        basket = self.store().get_basket()
        if basket.is_empty():
            return []
        return basket.summary(currency="INR", exponent=2)

    def send(self, actions, *, tenant=None, platform="telegram"):
        tenant = tenant or self.tenant
        self.classify.return_value = classification_result([
            (action, "general", "cancel_and_abort", None, None) if action == "cancel"
            else (action, "scripted", action, None, None) for action in actions
        ])
        pending, _ = self.store(tenant, platform).get_ongoing_queries()
        if pending and (self.reply_to_pending or 'cancel' in actions):
            self.classify.return_value = classification_result([(*row[:3], str(pending[-1].query_id), None)
                for row in classification_rows(self.classify.return_value)])
        handler = self.handler_module.CafeHandler(tenant, self.store(tenant, platform))
        return handler.handle_message(" and ".join(actions), SimpleNamespace(pk=1, tenant_id=tenant.id, addresses=MagicMock()))

    def test_multiple_intents_preserve_reply_and_basket_operation_order(self):
        response, basket = self.send(["add", "update"])
        self.assertEqual(response, "add reply. update reply")
        self.assertEqual(ScriptedIntent.operations, ["add", "update"])
        self.assertEqual(basket, [{"name": "latte", "size": "regular", "quantity": 3,
                                   "currency": "INR", "exponent": 2,
                                   "unit_price_minor": 10000, "line_total_minor": 30000}])
        self.classify.assert_called_once_with("add and update", "", "", tenant_key="1", conversation_context=ANY)
        self.assertEqual(self.persisted_summary(), basket)

    def test_followup_uses_saved_context_and_removes_completed_query(self):
        self.assertEqual(self.send(["ask"])[0].strip(), "Which size?")
        self.reply_to_pending = True
        self.assertEqual(self.send(["large"])[0], "followup reply")
        self.classify.assert_called_with("large", "Which size?", "ask", tenant_key="1", conversation_context=ANY)
        self.assertEqual(ScriptedIntent.operations, ["ask", "followup:large"])
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))
        self.assertEqual(len(self.store().get_history()), 2)

    def test_cancellation_removes_pending_query_without_business_handler(self):
        self.send(["ask"])
        self.assertIn("Stopped the current request", self.send(["cancel"])[0])
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))
        self.assertEqual(ScriptedIntent.operations, ["ask"])

    def test_cancel_without_pending_query_does_not_invoke_business_handler(self):
        response, _ = self.send(["cancel"])
        self.assertIn("no pending request", response)
        self.assertEqual(ScriptedIntent.operations, [])

    def test_new_query_handoff_is_saved_without_executing_target(self):
        response, _ = self.send(["handoff"], platform="whatsapp")
        self.assertEqual(response, "handoff reply. Confirm order?")
        pending, index = self.store(platform="whatsapp").get_ongoing_queries()
        self.assertEqual(index, 0)
        self.assertEqual((pending[0].tenant, pending[0].chat_id, pending[0].platform), (1, "same-user", "whatsapp"))
        self.assertEqual(pending[0].sub_intent, "confirm")
        self.assertEqual(ScriptedIntent.operations, ["handoff"])
        self.reply_to_pending = True
        self.assertEqual(self.send(["yes"], platform="whatsapp")[0], "followup reply")
        self.assertEqual(self.store(platform="whatsapp").get_ongoing_queries(), ([], None))

    def test_followup_can_handoff_to_next_pending_query(self):
        self.send(["ask"])
        self.reply_to_pending = True
        response, _ = self.send(["handoff"])
        self.assertEqual(response, "followup reply. Confirm order?")
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index, pending[0].sub_intent), (1, 0, "confirm"))

    def test_latest_incomplete_query_supplies_next_followup(self):
        self.send(["ask", "ask"])
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index), (2, 1))
        self.assertEqual(pending[0].follow_up_question, ["Which size?"])
        self.assertEqual(pending[1].follow_up_question, ["Which size?"])

    def test_unmatched_prompt_is_preserved_after_new_query(self):
        self.send(["ask"])
        self.assertEqual(self.send(["add"])[0], "add reply")
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index, pending[0].main_query), (1, 0, 'ask'))

    def test_session_roundtrip_preserves_all_conversation_fields(self):
        self.send(["add", "save", "ask"])
        restored = self.store()
        self.assertEqual(restored.get_counter(), 1)
        self.assertEqual(len(restored.get_history()), 3)
        self.assertEqual(restored.get_delivery_address(), {"city": "Delhi"})
        self.assertTrue(restored.get_checklist()["location"])
        handler = self.handler_module.CafeHandler(self.tenant, restored)
        self.assertEqual(handler.get_basket()[0]["quantity"], 1)
        pending, index = restored.get_ongoing_queries()
        self.assertEqual(index, 0)
        self.assertEqual(pending[0].main_query, "ask")

    def test_same_user_has_independent_conversations_in_two_tenants(self):
        other = SimpleNamespace(id=2, pk=2, api_key="tenant-2")
        self.send(["add", "save", "ask"])
        self.assertEqual(self.store(other).get_history(), [])
        self.assertEqual(self.store(other).get_delivery_address(), {})
        self.assertFalse(self.store(other).get_checklist()["location"])
        self.assertEqual(self.store(other).get_ongoing_queries(), ([], None))
        response, basket = self.send(["add", "update"], tenant=other)
        self.assertEqual(basket[0]["quantity"], 3)
        self.classify.assert_called_with("add and update", "", "", tenant_key="2", conversation_context=ANY)
        self.assertEqual(self.persisted_summary()[0]["quantity"], 1)
        self.reply_to_pending = True
        self.send(["large"])
        self.assertEqual(self.store().get_counter(), 2)
        self.assertEqual(self.store(other).get_counter(), 1)
        self.assertEqual(self.store(other).get_ongoing_queries(), ([], None))
        self.assertEqual(len(set(self.redis.lock_names)), 2)

    def test_same_user_has_independent_platform_sessions(self):
        self.send(["ask"])
        self.send(["add"], platform="whatsapp")
        self.classify.assert_called_with("add", "", "", tenant_key="1", conversation_context=ANY)
        self.assertEqual(self.persisted_summary(), [])
        self.assertEqual(len(self.store().get_ongoing_queries()[0]), 1)
        self.lookup.assert_called_with("same-user", tenant_id=1, platform="whatsapp")

    def test_new_chat_session_uses_actual_tenant_and_platform(self):
        self.lookup.return_value = None
        self.send(["ask"], platform="whatsapp")
        kwargs = self.create.call_args.kwargs
        self.assertEqual((kwargs["tenant"], kwargs["platform"], kwargs["session_id"]),
                         (self.tenant, "whatsapp", "same-user"))

    def test_combined_failure_preserves_pending_state_and_skips_business_operations(self):
        self.send(["ask"])
        before = self.store().get_ongoing_queries()[0][0].to_dict()
        self.classify.reset_mock()
        from chatbot_core.logic.cafe.prompts.normalize_and_classify import NormalizationClassificationError
        self.classify.side_effect = NormalizationClassificationError("Invalid output")
        response, basket = self.send(["large"])
        self.assertIn("ask again", response)
        self.assertIsNone(basket)
        self.classify.assert_called_once()
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((pending[0].to_dict(), index), (before, 0))
        self.assertEqual(ScriptedIntent.operations, ["ask"])

    def test_classification_failure_fallback_does_not_mutate_basket(self):
        self.send(["add"])
        classifier = importlib.import_module("chatbot_core.logic.cafe.prompts.normalize_and_classify")
        self.classify.side_effect = classifier.normalize_and_classify
        with patch.object(classifier, "_cached_proposal", return_value=None), \
             patch.object(classifier, "get_intent_classification_cache", return_value={}), \
             patch.object(classifier, "structured_chain", side_effect=RuntimeError("Provider unavailable")), \
             patch.object(classifier.cache, "set") as save, \
             self.assertLogs(classifier.logger, level="ERROR"):
            response, _ = self.send(["unknown"])
        self.assertIn("ask again", response)
        save.assert_not_called()
        self.assertEqual(self.persisted_summary()[0]["quantity"], 1)
        self.assertEqual(self.store().get_counter(), 2)

    def test_business_knowledge_helper_includes_full_scope(self):
        intent = ScriptedIntent(main_query="question", sub_intent="order_status", tenant=1, chat_id="same-user")
        intent.platform = "telegram"
        with patch("chatbot_core.logic.cafe.prompts.answer_from_knowledge.answer_from_knowledge", return_value="answer") as answer:
            self.assertEqual(intent.answer_from_knowledge("private order", "question"), "answer")
        answer.assert_called_once_with("private order", "question", tenant_key="1", user_id="same-user",
                                       platform="telegram", sub_intent="order_status", main_intent="scripted")

    def test_saved_intent_from_other_tenant_is_rejected(self):
        wrong = ScriptedIntent(main_query="private", sub_intent="ask", tenant=2, chat_id="same-user")
        self.store().set_ongoing_queries([wrong], 0)
        with self.assertRaisesRegex(ValueError, "Saved intent"):
            self.handler_module.CafeHandler(self.tenant, self.store()).handle_message("hi")

    def test_business_exception_is_not_retried(self):
        with self.assertRaisesRegex(RuntimeError, "Business operation failed"):
            self.send(["fail", "add"])
        self.assertEqual(ScriptedIntent.operations, ["fail"])
        self.assertEqual(self.persisted_summary(), [])

    def test_wrong_tenant_store_or_customer_is_rejected(self):
        other = SimpleNamespace(id=2, pk=2, api_key="tenant-2")
        with self.assertRaisesRegex(ValueError, "Session tenant"):
            self.handler_module.CafeHandler(other, self.store())
        handler = self.handler_module.CafeHandler(self.tenant, self.store())
        with self.assertRaisesRegex(ValueError, "Customer tenant"):
            handler.handle_message("hello", SimpleNamespace(pk=1, tenant_id=2))
        self.lookup.assert_not_called()

    def test_independent_intents_do_not_share_mutable_followup_state(self):
        first = ScriptedIntent(main_query="one", sub_intent="ask", tenant=1, chat_id="same-user")
        second = ScriptedIntent(main_query="two", sub_intent="ask", tenant=2, chat_id="same-user")
        first.follow_up_reply.append("private")
        first.basket_item["name"] = "private"
        self.assertEqual(second.follow_up_reply, [])
        self.assertEqual(second.basket_item, {})

    def test_processor_passes_tenant_platform_and_chat_identity_to_store(self):
        with self.settings(CELERY_BROKER_URL="redis://localhost:6379/0"):
            processor = importlib.import_module("chatbot_core.processor")
        tenant = SimpleNamespace(id=2, telegram_bot_token='tenant-bot-token')
        customer = SimpleNamespace(id=3, tenant_id=2, name="User", phone="123")
        adapter = Mock()
        adapter.augment_text.side_effect = lambda text, payload: text
        routed = []

        def route(tenant, text, store, **kwargs):
            routed.append((tenant.id, store.tenant_id, store.platform, store.user_id))
            return "reply", []

        with patch.object(processor.TenantInfo.objects, "get", return_value=tenant), \
             patch.object(processor, "get_adapter", return_value=adapter), \
             patch.object(processor, "_call_create_or_get_customer_safe", return_value=customer), \
             patch.object(processor, "append_message"), patch.object(processor, "touch_active_chat"), \
             patch.object(processor, "set_latest_meta"), \
             patch.object(processor, "is_global_agent_enabled", return_value=True), \
             patch.object(processor, "is_agent_enabled", return_value=True), \
             patch.object(processor, "route_message_for_tenant", side_effect=route):
            processor.process_payload("2", "user", {"tenant_id": "2", "user_id": "user", "channel": "whatsapp", "chat_id": "chat", "text": "hi"})
            processor.process_payload("2", "user", {"tenant_id": "2", "user_id": "user", "channel": "telegram", "bot_token": "tenant-bot-token", "text": "hi"})
        self.assertEqual(routed, [(2, "2", "whatsapp", "chat"), (2, "2", "telegram", "user")])

    def test_large_intent_batch_finishes_without_recursion_cutoff(self):
        from commerce.policy import Policy, evaluation_policy
        loose = evaluation_policy()
        loose["ordering_limits"]["max_line_quantity"] = 40
        loose["ordering_limits"]["max_item_quantity"] = 40
        loose["ordering_limits"]["max_basket_units"] = 40
        with patch("chatbot_core.logic.cafe.ordering_limits.load_policy",
                   return_value=Policy.model_validate(loose)):
            response, basket = self.send(["add"] * 40)
        self.assertEqual(basket[0]["quantity"], 40)
        self.assertEqual(response, ". ".join(["add reply"] * 40))
        self.assertEqual(ScriptedIntent.operations, ["add"] * 40)
        self.assertEqual(len(self.store().get_history()), 40)

    def send_classified_with_general(self, rows):
        """Keep GeneralIntent and its helper real; fake only data and model I/O."""
        from django.core.cache import cache
        cache.clear()
        knowledge = importlib.import_module("chatbot_core.logic.cafe.prompts.generate_response_from_knowledge")
        resolve = lambda name: GeneralIntent if name == "general" else ScriptedIntent
        pending, _ = self.store().get_ongoing_queries()
        if pending:
            rows = [(*row[:3], str(pending[-1].query_id), None)
                    if row[2] == 'cancel_and_abort' or row[1] == 'scripted' and row[2] == 'large' else row for row in rows]
        self.classify.return_value = classification_result(rows)
        with patch.object(self.operations, "get_intent", side_effect=resolve), \
             patch.object(intent_base, "get_intent", side_effect=resolve), \
             patch.object(knowledge, "get_knowledge_base_cache", return_value={
                 (self.tenant.api_key, "general", sub): {"payload": "Be friendly."}
                 for sub in GeneralIntent.SUB_INTENT_NAMES
             }), patch.object(knowledge, "get_intent_prompt_cache", return_value={}), \
             patch.object(knowledge, "enqueue_string"), \
             patch.object(knowledge, "text_chain", return_value=Mock(invoke=Mock(return_value="General reply"))):
            return self.handler_module.CafeHandler(self.tenant, self.store()).handle_message(
                " and ".join(row[0] for row in rows), SimpleNamespace(pk=1, tenant_id=self.tenant.id, addresses=MagicMock()),
            )

    def test_real_general_wait_preserves_pending_state_and_resumes(self):
        self.send(["add", "ask"])
        basket = self.store().get_basket().to_dict()
        before, index = self.store().get_ongoing_queries()
        for _ in range(2):
            response, _ = self.send_classified_with_general([("Wait a moment", "general", "wait", None, None)])
            self.assertEqual(response, "General reply")
            pending, index = self.store().get_ongoing_queries()
            self.assertEqual(index, 0)
            self.assertEqual(pending[0].to_dict(), before[0].to_dict())
            self.assertEqual(self.store().get_basket().to_dict(), basket)
        self.reply_to_pending = True
        self.assertEqual(self.send(["large"])[0], "followup reply")
        self.classify.assert_called_with("large", "Which size?", "ask", tenant_key="1", conversation_context=ANY)
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))

    def test_real_general_wait_without_pending_query(self):
        self.assertEqual(self.send_classified_with_general([("Wait", "general", "wait", None, None)])[0], "General reply")
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))

    def test_real_general_cross_intent_followup_does_not_crash(self):
        self.send(["ask"])
        self.reply_to_pending = True
        self.assertEqual(self.send_classified_with_general([("Thanks", "general", "thanks", None, None)])[0], "General reply")
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index, pending[0].ignored_count), (1, 0, 0))

    def test_wait_then_answer_in_same_batch_does_not_drop_incomplete_query(self):
        self.send(["ask"])
        self.reply_to_pending = True
        def process(intent, query, *args):
            intent.follow_up_question.append("How many?")
            return "Need quantity", intent.query_id
        with patch.object(ScriptedIntent, "process_followup", process):
            response, _ = self.send_classified_with_general([
                ("Wait", "general", "wait", None, None), ("large", "scripted", "large", None, None),
            ])
        self.assertEqual(response, "General reply. Need quantity. How many?")
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index, pending[0].ignored_count), (1, 0, 0))

    def test_wait_then_cancel_clears_pending_query(self):
        self.send(["ask"])
        response, _ = self.send_classified_with_general([
            ("Wait", "general", "wait", None, None), ("Never mind", "general", "cancel_and_abort", None, None),
        ])
        self.assertIn("General reply. Stopped the current request.", response)
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))

    def test_wait_suppresses_new_handoff_question_without_losing_it(self):
        response, _ = self.send_classified_with_general([
            ("handoff", "scripted", "handoff", None, None), ("Wait", "general", "wait", None, None),
        ])
        self.assertEqual(response, "handoff reply. General reply")
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index, pending[0].get_followup_question()), (1, 0, "Confirm order?"))


    def test_incomplete_followup_moves_to_end_and_keeps_next_question(self):
        self.send(["ask", "ask"])
        self.reply_to_pending = True
        def process(intent, query, *args):
            intent.follow_up_question.append("How many?")
            return "Need quantity", intent.query_id
        with patch.object(ScriptedIntent, "process_followup", process):
            response, _ = self.send(["large"])
        self.assertEqual(response, "Need quantity. How many?")
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index), (2, 1))
        self.assertEqual(pending[0].get_followup_question(), "Which size?")
        self.assertEqual(pending[1].get_followup_question(), "How many?")
        # History captures the earlier question, without later mutations leaking in.
        self.assertEqual(self.store().get_history()[0]["query_obj"]["follow_up_question"], ["Which size?"])

    def test_reused_adapter_loads_fresh_state_and_does_not_repeat_handoff(self):
        handler = self.handler_module.CafeHandler(self.tenant, self.store())
        self.classify.return_value = classification_result([("handoff", "scripted", "handoff", None, None)])
        handler.handle_message("handoff")
        self.classify.return_value = classification_result([("yes", "scripted", "yes", str(self.store().get_ongoing_queries()[0][-1].query_id), None)])
        self.reply_to_pending = True
        self.assertEqual(handler.handle_message("yes")[0], "followup reply")
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))
        self.assertEqual(self.store().get_counter(), 2)

    def test_pending_followup_survives_new_adapter_and_session_store(self):
        self.send(["add", "ask"])
        self.reply_to_pending = True
        response, basket = self.send(["large"])
        self.assertEqual(response, "followup reply")
        self.assertEqual(basket[0]["quantity"], 1)
        self.classify.assert_called_with("large", "Which size?", "ask", tenant_key="1", conversation_context=ANY)
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))
        self.assertEqual(len(self.store().get_history()), 3)
        self.assertEqual(ScriptedIntent.operations, ["add", "ask", "followup:large"])

    def test_empty_classification_uses_graph_fallback_response(self):
        self.classify.side_effect = lambda *args, **kwargs: classification_result([])
        response, basket = self.send(["unknown"])
        self.assertIn("ask again", response)
        self.assertIsNone(basket)
        self.assertEqual(ScriptedIntent.operations, [])
        self.assertEqual(len(self.store().get_history()), 0)

    def test_last_handoff_is_selected_after_all_intents_finish(self):
        with patch.object(ScriptedIntent, "build_handoff_intent", autospec=True,
                          side_effect=intent_base.BaseIntent.build_handoff_intent) as handoff:
            self.send(["handoff", "add", "handoff"])
        self.assertEqual(handoff.call_count, 2)
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index), (1, 0))
        self.assertEqual(ScriptedIntent.operations, ["handoff", "add", "handoff"])
        self.assertEqual(self.persisted_summary()[0]["quantity"], 1)

    def test_cross_intent_reply_id_preserves_unrelated_pending_task(self):
        pending = ScriptedIntent(main_query="pending", sub_intent="ask", tenant=1,
                                 chat_id="same-user", query_id=100, follow_up_question=["Which size?"])
        pending.intent_type = "other_type"
        self.store().set_ongoing_queries([pending], 0)
        self.reply_to_pending = True
        original = ScriptedIntent.process_query
        restrictions = []
        def process(intent, *args):
            restrictions.append(intent.promp_restriction)
            return original(intent, *args)
        with patch.object(ScriptedIntent, "process_query", process):
            response, _ = self.send(["add"])
        self.assertEqual(restrictions, [True])
        self.assertEqual(response, "add reply")
        self.assertEqual([p.query_id for p in self.store().get_ongoing_queries()[0]], [100])

    def test_engine_does_not_retry_connection_errors_after_business_side_effect(self):
        side_effect = Mock()
        def process(intent, *args):
            side_effect()
            raise ConnectionError("Payment service unavailable")
        with patch.object(ScriptedIntent, "process_query", process):
            with self.assertRaisesRegex(ConnectionError, "Payment service"):
                self.send(["confirm", "add"])
        side_effect.assert_called_once_with()
        self.assertEqual(self.store().get_history(), [])

    def test_save_failure_does_not_execute_business_operations_again(self):
        with patch.object(redis_session.RedisSessionStore, "set_basket", side_effect=ConnectionError("Redis unavailable")) as save:
            with self.assertRaisesRegex(ConnectionError, "Redis unavailable"):
                self.send(["add"])
        self.assertEqual(ScriptedIntent.operations, ["add"])
        save.assert_called_once()


    def test_model_retry_does_not_repeat_surrounding_business_operation(self):
        import httpx
        from langchain_openai import ChatOpenAI
        from chatbot_core.llm.chains import text_chain

        requests = []
        side_effect = Mock()
        def respond(request):
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(500, json={"error": {"message": "temporary", "type": "server_error"}})
            return httpx.Response(200, json={
                "id": "offline", "object": "chat.completion", "created": 0, "model": "gpt-4.1-mini",
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": "Confirmed"}}],
            })
        def process(intent, *args):
            side_effect()
            reply = text_chain("Reply briefly").invoke({"input": "Confirm"})
            intent.is_complete = True
            return reply, intent.query_id

        with httpx.Client(transport=httpx.MockTransport(respond)) as client:
            model = ChatOpenAI(model="gpt-4.1-mini", api_key="offline", http_client=client, max_retries=1)
            with patch("chatbot_core.llm.chains.get_chat_model", return_value=model), \
                 patch.object(ScriptedIntent, "process_query", process):
                self.assertEqual(self.send(["confirm"])[0], "Confirmed")
        self.assertEqual(len(requests), 2)
        side_effect.assert_called_once_with()
        self.assertEqual(len(self.store().get_history()), 1)


class ConversationTests(ConversationContract, SimpleTestCase):
    def test_later_intents_see_current_turn_history_before_session_save(self):
        original = ScriptedIntent.process_query
        observed = []
        def process(intent, basket, address, checklist, history, api_key, customer):
            observed.append(([row["query_obj"]["sub_intent"] for row in history],
                             self.store().get_history()))
            return original(intent, basket, address, checklist, history, api_key, customer)
        with patch.object(ScriptedIntent, "process_query", process):
            self.send(["add", "update"])
        self.assertEqual(observed, [([], []), (["add"], [])])
        self.assertEqual(len(self.store().get_history()), 2)


    def test_failed_turn_does_not_mutate_memory_store_by_reference(self):
        with patch.object(memory, "_session_data", {}):
            store = memory.MemorySessionStore("same-user", tenant_id=1, platform="telegram")
            store.set_delivery_address({"city": "Original"})
            self.classify.return_value = classification_result([(action, "scripted", action, None, None) for action in ("save", "fail")])
            handler = self.handler_module.CafeHandler(self.tenant, store)
            with self.assertRaisesRegex(RuntimeError, "Business operation failed"):
                handler.handle_message("save and fail")
            self.assertEqual(store.get_delivery_address(), {"city": "Original"})
            self.assertEqual(store.get_history(), [])
            self.assertFalse(store.get_checklist()["location"])


class SessionIsolationTests(SimpleTestCase):
    def test_legacy_redis_data_is_not_adopted_or_overwritten(self):
        backend = FakeRedis()
        legacy = json.dumps({"chat_history": [{"private": "tenant unknown"}]})
        backend.data["session:same-user"] = legacy
        with patch.object(redis_session, "_redis", backend):
            store = redis_session.RedisSessionStore("same-user", tenant_id=1, platform="telegram")
            self.assertEqual(store.get_history(), [])
            store.set_history([{"private": "tenant one"}])
            reopened = redis_session.RedisSessionStore("same-user", tenant_id=1, platform="telegram")
            self.assertEqual(reopened.get_history(), [{"private": "tenant one"}])
        self.assertEqual(backend.data["session:same-user"], legacy)

    def test_scope_required_and_delimiters_do_not_collide(self):
        with self.assertRaises(ValueError):
            session_identity(None, "telegram", "one")
        with self.assertRaises(ValueError):
            session_identity(1, "", "one")
        with self.assertRaises(ValueError):
            session_identity(1, "telegram", None)
        self.assertNotEqual(session_identity("a:b", "c", "d"), session_identity("a", "b:c", "d"))
        self.assertEqual(session_identity(1, "web", "u"), session_identity("1", "website", "u"))

    def test_memory_sessions_are_scoped(self):
        with patch.object(memory, "_session_data", {}):
            one = memory.MemorySessionStore("user", tenant_id=1, platform="telegram")
            two = memory.MemorySessionStore("user", tenant_id=2, platform="telegram")
            three = memory.MemorySessionStore("user", tenant_id=1, platform="whatsapp")
            one.set_history([{"private": "one"}])
            self.assertEqual(two.get_history(), [])
            self.assertEqual(three.get_history(), [])

    def test_django_sessions_are_scoped_within_same_browser(self):
        class BrowserSession(dict):
            session_key = "same-browser"
            modified = False
        browser = BrowserSession(chat_history=[{"legacy": True}])
        request = SimpleNamespace(session=browser)
        one = DjangoSessionStore(request, tenant_id=1)
        two = DjangoSessionStore(request, tenant_id=2)
        one.set_history([{"private": "one"}])
        self.assertEqual(two.get_history(), [])
        self.assertEqual(DjangoSessionStore(request, tenant_id=1).get_history(), [{"private": "one"}])
        self.assertTrue(browser.modified)


class ChatSessionLookupTests(TestCase):
    def test_lookup_is_scoped_and_selects_latest_active_session(self):
        first = TenantInfo.objects.create(slug="one", display_name="One")
        second = TenantInfo.objects.create(slug="two", display_name="Two")
        customers = {t.id: Customer.objects.create(tenant=t, name="User", phone="123") for t in (first, second)}
        def create(tenant, platform="telegram", **kwargs):
            return ChatSession.objects.create(tenant=tenant, customer=customers[tenant.id],
                                              session_id="same-user", platform=platform, **kwargs)
        older = create(first)
        latest = create(first)
        other_tenant = create(second)
        other_platform = create(first, "whatsapp")
        create(first, is_completed=True)
        self.assertEqual(get_chat_ongoing_session("same-user", tenant_id=first.id, platform="telegram"), latest)
        self.assertNotEqual(older.pk, latest.pk)
        self.assertEqual(get_chat_ongoing_session("same-user", tenant_id=second.id, platform="telegram"), other_tenant)
        self.assertEqual(get_chat_ongoing_session("same-user", tenant_id=first.id, platform="whatsapp"), other_platform)
        self.assertIsNone(get_chat_ongoing_session("same-user", tenant_id=second.id, platform="website"))

        order = Order.objects.create(tenant=first, customer=customers[first.id], source="inhouse", total_amount=100)
        linked = update_chat_session_order(first, "same-user", order, platform="whatsapp")
        self.assertEqual(linked.pk, other_platform.pk)
        latest.refresh_from_db()
        other_tenant.refresh_from_db()
        self.assertIsNone(latest.order_id)
        self.assertIsNone(other_tenant.order_id)

    def test_intent_schema_loader_does_not_merge_tenant_labels(self):
        tenants = [TenantInfo.objects.create(slug=slug, display_name=slug) for slug in ("one", "two")]
        for tenant in tenants:
            TenantJSONDoc.objects.create(tenant=tenant, dtype="intent_classification", intent="general",
                                         sub_intent="greeting", payload=tenant.slug)
        from chatbot_core.runtime_configuration import publish_configuration
        for tenant in tenants:
            TenantJSONDoc.objects.create(tenant=tenant, dtype="response_intents", intent="general",
                                         sub_intent="greeting", payload="Greet the customer.")
            publish_configuration(tenant.pk, expected_version=0)
        knowledge_cache.load_intent_classification_cache()
        self.assertEqual(knowledge_cache.get_intent_classification_cache(tenants[0].id),
                         {"general": {"greeting": {"description": "one", "examples": []}}})
        self.assertEqual(knowledge_cache.get_intent_classification_cache(str(tenants[1].id)),
                         {"general": {"greeting": {"description": "two", "examples": []}}})
        self.assertEqual(knowledge_cache.get_intent_classification_cache("missing"), {})
