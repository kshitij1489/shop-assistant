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
from chatbot_core.logic.cafe import reply_renderer
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
        self.chain.stream.side_effect = AssertionError('Handler drafts must not stream')
        self.chain.invoke.return_value = "Hello café!"
        self.enterContext(patch.object(self.knowledge, "text_chain", return_value=self.chain))
        self.renderer = Mock()
        self.renderer.invoke.side_effect = self.compose
        self.enterContext(patch.object(reply_renderer, 'structured_chain', return_value=self.renderer))
        self.enterContext(patch("chatbot_core.llm.chains.get_chat_model", side_effect=AssertionError("Unexpected provider I/O")))

    @staticmethod
    def compose(values):
        context = json.loads(values['input'])
        followup = context['permitted_followup'] or {}
        return reply_renderer.RenderedReply(response=context['verified_reply'],
                                            question=followup.get('question', ''))

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

    def test_validated_reply_then_commit_and_cached_knowledge_reuse(self):
        cache.clear()
        self.chain.reset_mock()
        response = self.send()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["X-Accel-Buffering"], "no")
        self.assertIn("sessionid", response.cookies)
        events = self.consume(response)
        self.assertEqual(events, [("replace", {"text": "Hello café!"}),
                                  ("done", {"response": "Hello café!", "basket": []})])
        chat = ChatSession.objects.get()
        self.assertEqual(chat.session_id, self.client.session.session_key)
        stored = self.client.session
        namespace = next(key for key in stored.keys() if key.startswith("cafe:v2:"))
        self.assertEqual(stored[namespace]["chat_history"][-1]["query_obj"]["response"], "Hello café!")
        self.assertEqual(self.consume(self.send()), events)
        self.chain.stream.assert_not_called()
        self.chain.invoke.assert_called_once()
        self.assertEqual(self.renderer.invoke.call_count, 2)
        self.assertEqual(Customer.objects.count(), 1)

    def test_multiple_intents_publish_one_composed_reply(self):
        self.classifications = [("thanks", "general", "thanks", None, None), ("hello", "general", "greeting", None, None)]
        cache.clear()
        self.chain.reset_mock()
        self.chain.invoke.side_effect = ['Earlier reply', 'Hello café!']
        events = self.consume(self.send())
        self.assertEqual(events, [('replace', {'text': 'Earlier reply. Hello café!'}),
                                  ('done', {'response': 'Earlier reply. Hello café!', 'basket': []})])
        self.assertEqual(self.chain.invoke.call_count, 2)
        self.chain.stream.assert_not_called()
        self.renderer.invoke.assert_called_once()

    def test_generated_clarification_is_published_once_and_saved_as_delivered(self):
        clarification = importlib.import_module("chatbot_core.logic.cafe.prompts.clarify_user_message")
        self.classifications = [("unclear", "insufficient_information", "insufficient_information", None, None)]
        self.chain.invoke.return_value = 'Could you clarify?'
        self.renderer.invoke.side_effect = None
        self.renderer.invoke.return_value = reply_renderer.RenderedReply(
            response='What would you like help with?', question='What would you like help with?')
        with patch.object(clarification, "text_chain", return_value=self.chain):
            self.client = Client()
            events = self.consume(self.send())
            self.assertEqual(events, [('replace', {'text': 'What would you like help with?'}),
                                      ('done', {'response': 'What would you like help with?', 'basket': []})])
        stored = self.client.session
        namespace = next(key for key in stored.keys() if key.startswith('cafe:v2:'))
        self.assertEqual(stored[namespace]['checklist']['last_assistant_question'],
                         'What would you like help with?')
        self.chain.stream.assert_not_called()

    def test_empty_classification_returns_clarification_text(self):
        clarification = importlib.import_module("chatbot_core.logic.cafe.prompts.clarify_user_message")
        self.classifications = []
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
        commits = []

        def publish(store, data):
            if not release.wait(5):
                raise AssertionError('Test did not release session commit')
            original_publish(store, data)
            commits.append(data)
            published.set()

        with patch.object(DjangoSessionStore, "publish_snapshot", publish):
            response = self.send()
            content = iter(response.streaming_content)
            self.assertEqual(self.decode(next(content)), ('replace', {'text': 'Hello café!'}))
            self.assertFalse(published.is_set())
            response.close()
            release.set()
            self.assertTrue(published.wait(5))
        self.assertEqual(len(commits), 1)
        self.chain.invoke.assert_called_once()
        self.renderer.invoke.assert_called_once()
        self.chain.stream.assert_not_called()
        stored = self.client.session
        namespace = next(key for key in stored.keys() if key.startswith("cafe:v2:"))
        self.assertEqual(stored[namespace]["chat_history"][-1]["query_obj"]["response"], "Hello café!")

    def test_provider_failure_publishes_uncached_fallback_then_recovers(self):
        self.chain.invoke.side_effect = RuntimeError('private provider failure')
        with self.assertLogs(self.knowledge.logger, level="ERROR"):
            events = self.consume(self.send())
        fallback = 'Hello! How can I help you today?'
        self.assertEqual(events, [('replace', {'text': fallback}),
                                  ('done', {'response': fallback, 'basket': []})])
        self.chain.invoke.side_effect = None
        self.chain.invoke.return_value = 'Recovered'
        self.assertEqual(self.consume(self.send())[-1][1]["response"], "Recovered")
        self.assertEqual(self.chain.invoke.call_count, 2)
        self.chain.stream.assert_not_called()

    def test_invalid_composition_never_reaches_stream_or_saved_exchange(self):
        self.renderer.invoke.side_effect = None
        self.renderer.invoke.return_value = reply_renderer.RenderedReply(
            response='Hello café! Pay INR 999.', question='')
        with self.assertLogs(reply_renderer.logger, level='ERROR'):
            events = self.consume(self.send())
        self.assertEqual(events, [('replace', {'text': 'Hello café!'}),
                                  ('done', {'response': 'Hello café!', 'basket': []})])
        stored = self.client.session
        namespace = next(key for key in stored.keys() if key.startswith('cafe:v2:'))
        self.assertEqual(stored[namespace]['checklist']['last_assistant_message'], 'Hello café!')
        self.renderer.invoke.assert_called_once()
        self.chain.invoke.assert_called_once()

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
