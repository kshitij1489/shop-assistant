"""Streaming HTTP/session contracts with real producer threads and the conversation graph."""
from tests.support.runtime import classification_result
import importlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from threading import Event
from unittest.mock import Mock, patch

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import Client, TransactionTestCase, override_settings

from chatbot_core.channels import website
from chatbot_core.channels.utils import generate_tenant_jwt
from chatbot_core.logic.cafe.session.django import DjangoSessionStore
from chatbot_core.models import TenantInfo
from orders.models import ChatSession, Customer
from tests.support.runtime import enable_legacy_capabilities


@override_settings(
    ROOT_URLCONF="tests.integration.test_website",
    JWT_SECRET="website-tests-only-secret-at-least-32-characters",
    SESSION_ENGINE="django.contrib.sessions.backends.db",
    SESSION_SAVE_EVERY_REQUEST=True,
    MIDDLEWARE=["django.contrib.sessions.middleware.SessionMiddleware"],
)
class WebsiteStreamingTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        self.tenant = TenantInfo.objects.create(slug="stream", display_name="Stream cafe",
                                                business_type="cafe", approval_status="APPROVED")
        enable_legacy_capabilities(self.tenant)
        self.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")
        self.knowledge = importlib.import_module("chatbot_core.logic.cafe.prompts.generate_response_from_knowledge")
        self.classifications = [("hello", "general", "greeting", None, None)]
        self.enterContext(patch.object(self.graph, "normalize_and_classify", side_effect=lambda *a, **k: classification_result(self.classifications)))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.enterContext(patch("chatbot_core.logic.cafe.workflow.runner.enqueue_string"))
        self.enterContext(patch.object(self.knowledge, "enqueue_string"))
        self.enterContext(patch.object(self.knowledge, "get_intent_prompt_cache", return_value={}))
        self.enterContext(patch.object(self.knowledge, "get_knowledge_base_cache", return_value={
            (self.tenant.api_key, "general", "greeting"): {"payload": "Welcome guests."},
            (self.tenant.api_key, "general", "thanks"): {"payload": "Thank guests."},
        }))
        self.chain = Mock()
        self.chain.stream.side_effect = lambda *a, **k: iter(["Hello", " café!"])
        self.chain.invoke.return_value = "Earlier reply"
        self.enterContext(patch.object(self.knowledge, "text_chain", return_value=self.chain))
        self.enterContext(patch("chatbot_core.llm.chains.get_chat_model", side_effect=AssertionError("Unexpected provider I/O")))

    def send(self):
        response = self.client.post("/chatbot-api/", {"message": "hello"}, content_type="application/json",
                                    HTTP_ACCEPT="text/event-stream",
                                    HTTP_AUTHORIZATION="Bearer " + generate_tenant_jwt(self.tenant.slug))
        self.addCleanup(response.close)
        return response

    @staticmethod
    def decode(frame):
        lines = frame.decode().splitlines()
        return lines[0][7:], json.loads(lines[1][6:])

    def consume(self, response):
        return [self.decode(frame) for frame in response.streaming_content]

    def test_graph_stream_then_commit_and_cache_complete_reply(self):
        cache.clear()
        self.chain.reset_mock()
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertIn("sessionid", response.cookies)
        events = self.consume(response)
        self.assertEqual(events, [("replace", {"text": ""}), ("delta", {"text": "Hello"}),
                                  ("delta", {"text": " café!"}),
                                  ("done", {"response": "Hello café!", "basket": []})])
        chat = ChatSession.objects.get()
        self.assertEqual(chat.session_id, self.client.session.session_key)
        stored = self.client.session
        namespace = next(key for key in stored.keys() if key.startswith("cafe:v2:"))
        self.assertEqual(stored[namespace]["chat_history"][-1]["query_obj"]["response"], "Hello café!")
        self.assertEqual(self.consume(self.send()), [("done", {"response": "Hello café!", "basket": []})])
        self.chain.stream.assert_called_once()
        self.chain.invoke.assert_not_called()
        self.assertEqual(Customer.objects.count(), 1)

    def test_only_last_intent_streams_with_earlier_reply_prefix(self):
        self.classifications = [("thanks", "general", "thanks", None, None), ("hello", "general", "greeting", None, None)]
        cache.clear()
        self.chain.reset_mock()
        events = self.consume(self.send())
        self.assertEqual(events[0], ("replace", {"text": "Earlier reply. "}))
        self.assertEqual(events[-1][1]["response"], "Earlier reply. Hello café!")
        self.chain.invoke.assert_called_once()
        self.chain.stream.assert_called_once()

    def test_generated_clarification_streams_and_final_question_is_not_duplicated(self):
        clarification = importlib.import_module("chatbot_core.logic.cafe.prompts.clarify_user_message")
        self.classifications = [("unclear", "insufficient_information", "insufficient_information", None, None)]
        self.chain.stream.side_effect = lambda *a, **k: iter(["Could you ", "clarify?"])
        with patch.object(clarification, "text_chain", return_value=self.chain):
            self.client = Client()
            events = self.consume(self.send())
            self.assertEqual(events[1], ("delta", {"text": "Could you "}))
            self.assertEqual(events[-1][1]["response"].strip(), "Could you clarify?")

    def test_empty_classification_returns_clarification_text(self):
        clarification = importlib.import_module("chatbot_core.logic.cafe.prompts.clarify_user_message")
        self.classifications = []
        self.chain.stream.side_effect = lambda *a, **k: iter(["Could you ", "clarify?"])
        self.chain.invoke.return_value = "Could you clarify?"
        with patch.object(clarification, "text_chain", return_value=self.chain):
            self.client = Client()
            events = self.consume(self.send())
            self.assertEqual(events[-1][0], "done")
            self.assertIn("ask again", events[-1][1]["response"])

    def test_text_is_visible_before_commit_and_disconnect_still_commits_once(self):
        release, published = Event(), Event()
        self.addCleanup(release.set)
        original_publish = DjangoSessionStore.publish_snapshot

        def publish(store, data):
            original_publish(store, data)
            published.set()

        def chunks(*args, **kwargs):
            yield "Hello"
            if not release.wait(5):
                raise AssertionError("Test did not release model")
            yield " café!"

        self.chain.stream.side_effect = chunks
        with patch.object(DjangoSessionStore, "publish_snapshot", publish):
            response = self.send()
            content = iter(response.streaming_content)
            self.assertEqual(self.decode(next(content))[0], "replace")
            self.assertEqual(self.decode(next(content)), ("delta", {"text": "Hello"}))
            self.assertFalse(published.is_set())
            response.close()
            release.set()
            self.assertTrue(published.wait(5))
        self.chain.stream.assert_called_once()
        stored = self.client.session
        namespace = next(key for key in stored.keys() if key.startswith("cafe:v2:"))
        self.assertEqual(stored[namespace]["chat_history"][-1]["query_obj"]["response"], "Hello café!")

    def test_partial_provider_failure_replaces_text_with_uncached_fallback(self):
        def broken(*args, **kwargs):
            yield "Partial answer"
            raise RuntimeError("private provider failure")

        self.chain.stream.side_effect = broken
        with self.assertLogs(self.knowledge.logger, level="ERROR"):
            events = self.consume(self.send())
        self.assertEqual(events[-2], ("replace", {"text": ""}))
        self.assertEqual(events[-1][0], "done")
        self.assertEqual(events[-1][1]["response"], "Hello! How can I help you today?")
        self.chain.stream.side_effect = lambda *a, **k: iter(["Recovered"])
        self.assertEqual(self.consume(self.send())[-1][1]["response"], "Recovered")
        self.assertEqual(self.chain.stream.call_count, 2)

    def test_failed_turn_emits_error_without_retry_or_session_commit(self):
        with patch.object(website, "route_message_for_tenant", side_effect=RuntimeError("private failure")) as route, \
                self.assertLogs("chatbot_core.channels.streaming", level="ERROR"):
            response = self.send()
            events = self.consume(response)
        self.assertEqual([event for event, _ in events], ["error"])
        self.assertNotIn("private failure", str(events))
        self.assertEqual(dict(self.client.session), {})
        route.assert_called_once()

    def test_auth_and_validation_errors_still_return_json(self):
        response = self.client.post("/chatbot-api/", {"message": "hi"}, HTTP_ACCEPT="text/event-stream")
        self.assertEqual(response.status_code, 401)
        self.assertIn("application/json", response["Content-Type"])
        self.assertFalse(Customer.objects.exists())

    def test_early_middleware_save_cannot_overwrite_newer_session_snapshot(self):
        self.consume(self.send())
        original = website.stream_turn

        def newer_snapshot(work):
            session = SessionStore(self.client.session.session_key)
            session["another_request"] = "keep me"
            session.save()
            return original(work)

        with patch.object(website, "stream_turn", newer_snapshot):
            response = self.send()
            self.assertEqual(Session.objects.get(session_key=self.client.session.session_key).get_decoded()["another_request"], "keep me")
            self.consume(response)
        self.assertEqual(self.client.session["another_request"], "keep me")

    def test_worker_inherits_request_context(self):
        context = ContextVar("test_request_context", default=None)
        token = context.set("owned-request")
        try:
            with patch.object(website, "route_message_for_tenant", side_effect=lambda *a, **k: (context.get(), [])):
                response = self.send()
                context.reset(token)
                token = None
                self.assertEqual(self.consume(response)[-1][1]["response"], "owned-request")
        finally:
            if token is not None:
                context.reset(token)

    def test_overlapping_streams_serialize_same_browser_turns(self):
        self.consume(self.send())
        entered, release, second_entered = Event(), Event(), Event()
        self.addCleanup(release.set)
        calls = []

        def route(tenant, message, store, **kwargs):
            calls.append(store.get_counter())
            if len(calls) == 1:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release first turn")
            else:
                second_entered.set()
            store.increment_counter()
            return str(store.get_counter()), []

        with patch.object(website, "route_message_for_tenant", route):
            first, second = self.send(), self.send()
            with ThreadPoolExecutor(max_workers=2) as pool:
                one = pool.submit(self.consume, first)
                self.assertTrue(entered.wait(3))
                two = pool.submit(self.consume, second)
                try:
                    self.assertFalse(second_entered.wait(0.1))
                finally:
                    release.set()
                self.assertEqual(one.result(5)[-1][1]["response"], "2")
                self.assertEqual(two.result(5)[-1][1]["response"], "3")
        self.assertEqual(calls, [1, 2])

    def test_unserializable_completion_emits_error_instead_of_hanging(self):
        with patch.object(website, "route_message_for_tenant", return_value=("Answer", object())), \
                self.assertLogs("chatbot_core.channels.streaming", level="ERROR"):
            self.assertEqual([event for event, _ in self.consume(self.send())], ["error"])
