"""In-process HTTP adapter through Django's real middleware and website view.

Remote HTTP integration can use ownership.ticket() with the same owned browser
cookie; do not use LocalRuntime.turn(), which substitutes successful services.
"""
import json
from threading import Lock
from time import perf_counter
from django.conf import settings
from django.test import Client
from evaluate.contracts.interfaces import ChatResponse
from .ownership import context_for, ticket


class ApplicationTransport:
    def __init__(self, provisioner):
        self.provisioner = provisioner
        self.clients = {}
        self.lock = Lock()

    def prepare(self, lease):
        pass  # Credentials are resolved immediately before each dispatch.

    def send(self, lease, request):
        from chatbot_core.channels.utils import generate_tenant_jwt
        context_for(lease, request.identity, request.request_id, self.provisioner)
        binding = self.provisioner.binding(lease)
        with self.lock:
            if lease.handle not in self.clients:
                client = Client(raise_request_exception=False)
                client.cookies[settings.SESSION_COOKIE_NAME] = binding['browser_session']
                self.clients[lease.handle] = client, Lock()
            client, lock = self.clients[lease.handle]
        with lock:
            signed = ticket(lease, request.identity, request.request_id, self.provisioner)
            started = perf_counter()
            response = client.post('/agent_core/chatbot-api/', data=json.dumps({'message': request.turn.text}),
                content_type='application/json', HTTP_X_EVALUATION_CONTEXT=signed,
                HTTP_AUTHORIZATION='Bearer ' + generate_tenant_jwt(binding['tenant'].slug))
            elapsed = (perf_counter() - started) * 1000
            try:
                payload = response.json()
            except (ValueError, TypeError):
                return ChatResponse(response.status_code, None, elapsed, 'invalid_response')
            return ChatResponse(response.status_code, payload.get('response'), elapsed)

    def close(self, lease):
        with self.lock:
            self.clients.pop(lease.handle, None)
