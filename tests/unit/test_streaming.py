"""Real LangChain/OpenAI streaming over an offline HTTP transport."""
import json
from unittest.mock import Mock

import httpx
from django.test import SimpleTestCase

from chatbot_core.llm.chains import text_chain, structured_chain
from chatbot_core.llm.schemas import FollowupDecision
from chatbot_core.llm.streaming import final_reply, invoke_reply, reply_stream
from tests.support.llm import ProviderHarness


class ReplyStreamingTests(ProviderHarness, SimpleTestCase):
    def respond(self, request):
        body = json.loads(request.content)
        if not body.get("stream"):
            return super().respond(request)
        self.requests.append(body)
        frames = []
        for text in ("Hello", " café", "!"):
            chunk = {"id": "offline", "object": "chat.completion.chunk", "created": 0,
                     "model": "gpt-4.1-mini", "choices": [
                         {"index": 0, "delta": {"content": text}, "finish_reason": None}]}
            frames.append("data: " + json.dumps(chunk) + "\n\n")
        frames.append('data: [DONE]\n\n')
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              content="".join(frames))

    def test_only_answer_call_streams_and_returns_complete_text(self):
        events = []
        with reply_stream(lambda event, data: events.append((event, data))), final_reply(True, []):
            self.assertTrue(structured_chain(FollowupDecision, "Classify").invoke({"input": "yes"}).is_followup)
            self.assertEqual(events, [])
            result = invoke_reply(text_chain("Answer"), {"input": "hello"})
        self.assertEqual(result, "Hello café!")
        self.assertFalse(self.requests[0].get("stream", False))
        self.assertTrue(self.requests[1]["stream"])
        self.assertEqual(events, [("replace", {"text": ""}), ("delta", {"text": "Hello"}),
                                  ("delta", {"text": " café"}), ("delta", {"text": "!"})])

    def test_non_final_intents_and_other_channels_keep_invoke(self):
        self.payload = "Hello!"
        events = []
        with reply_stream(lambda *event: events.append(event)), final_reply(False, []):
            self.assertEqual(invoke_reply(text_chain("Answer"), {"input": "hello"}), "Hello!")
        with final_reply(True, []):
            self.assertEqual(invoke_reply(text_chain("Answer"), {"input": "hello"}), "Hello!")
        self.assertEqual(events, [])
        self.assertTrue(all(not request.get("stream") for request in self.requests))

    def test_failed_partial_stream_is_cleared_and_closed(self):
        closed = []

        def broken():
            try:
                yield "Incomplete"
                raise RuntimeError("provider failed")
            finally:
                closed.append(True)

        chain = Mock()
        chain.stream.return_value = broken()
        events = []
        with reply_stream(lambda *event: events.append(event)), final_reply(True, ["Earlier reply"]):
            with self.assertRaises(RuntimeError):
                invoke_reply(chain, {})
        self.assertEqual(events, [("replace", {"text": "Earlier reply. "}),
                                  ("delta", {"text": "Incomplete"}),
                                  ("replace", {"text": "Earlier reply. "})])
        self.assertEqual(closed, [True])
        chain.invoke.assert_not_called()
