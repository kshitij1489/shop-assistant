"""Real LangChain calls over an offline HTTP transport."""
import json
from unittest.mock import patch

import httpx
from django.core.cache import cache
from langchain_openai import ChatOpenAI


class ProviderHarness:
    def setUp(self):
        cache.clear()
        self.payload = {"is_followup": True}
        self.requests = []
        self.status = 200
        self.client = httpx.Client(transport=httpx.MockTransport(self.respond))
        self.addCleanup(self.client.close)
        model = ChatOpenAI(
            model="gpt-4.1-mini", api_key="offline-tests-only",
            http_client=self.client, max_retries=0,
        )
        self.factory = self.enterContext(patch("chatbot_core.llm.chains.get_chat_model", return_value=model))

    def respond(self, request):
        self.requests.append(json.loads(request.content))
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": "unavailable", "type": "server_error"}})
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "gpt-4.1-mini",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": content,
            }}],
        })
