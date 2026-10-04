"""Controlled stand-in for the website chat API used by runner tests.

Reproduces the observable contract of `chatbot_core/channels/website.py`: an API
key exchanges for a tenant token, the chat endpoint needs a Bearer token, blank
messages get HTTP 400, and a `sessionid` cookie scopes the guest conversation.
Fault directives are embedded in message text so tests stay declarative:

    "[hang]"        record the message, then sleep past the client timeout
    "[hang-once]"   hang only the first time this exact text is seen
    "[status-500]"  respond with an internal error after recording the message
    "[disconnect]"  record the message and close the socket without a response
    "[slow:0.2]"    hold the request for 0.2 seconds before replying
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

SYNTHETIC_TOKEN_PREFIX = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."


@dataclass
class SessionState:
    session_id: str
    customer_id: str
    messages: list[str] = field(default_factory=list)


@dataclass
class RequestSpan:
    session_id: str
    message: str
    started: float
    finished: float


class ChatServerState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.api_keys: dict[str, str] = {}  # tenant slug -> API key
        self.tokens: dict[str, str] = {}  # token -> tenant slug
        self.sessions: dict[str, SessionState] = {}
        self.spans: list[RequestSpan] = []
        self.hung_once: set[str] = set()
        self.token_requests = 0
        self.evaluation_headers: list[str | None] = []
        self.cookie_headers: list[str | None] = []
        self.reject_next_chat = False  # one 401 before the message is handled, then accept
        self.in_flight = 0
        self.max_in_flight = 0
        self.hang_seconds = 2.0

    def register_tenant(self, slug: str, api_key: str) -> None:
        with self.lock:
            self.api_keys[slug] = api_key

    def issue_token(self, slug: str) -> str:
        token = f"{SYNTHETIC_TOKEN_PREFIX}{uuid4().hex}{uuid4().hex}.{uuid4().hex}"
        with self.lock:
            self.tokens[token] = slug
            self.token_requests += 1
        return token

    def session_for(self, session_id: str | None) -> tuple[SessionState, bool]:
        with self.lock:
            if session_id and session_id in self.sessions:
                return self.sessions[session_id], False
            new_id = uuid4().hex
            state = self.sessions[new_id] = SessionState(new_id, f"customer-{uuid4().hex[:8]}")
            return state, True

    def enter(self) -> None:
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)

    def leave(self, span: RequestSpan) -> None:
        with self.lock:
            self.in_flight -= 1
            self.spans.append(span)


class _Handler(BaseHTTPRequestHandler):
    state: ChatServerState

    def log_message(self, *_args) -> None:  # keep test output quiet
        return

    def _json(self, status: int, payload: dict, cookie: str | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", f"sessionid={cookie}; Path=/; HttpOnly")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path != "/agent_core/token/":
            return self._json(404, {"error": "not found"})
        slug = parse_qs(url.query).get("tenant", [""])[0]
        if not slug:
            return self._json(400, {"error": "Missing tenant"})
        api_key = self.headers.get("X-API-KEY")
        if not api_key:
            return self._json(401, {"error": "Missing API key"})
        if slug not in self.state.api_keys:
            return self._json(404, {"error": "Invalid tenant"})
        if self.state.api_keys[slug] != api_key:
            return self._json(403, {"error": "Invalid API key"})
        return self._json(200, {"token": self.state.issue_token(slug)})

    def do_POST(self) -> None:
        if self.path != "/agent_core/chatbot-api/":
            return self._json(404, {"error": "not found"})
        with self.state.lock:
            self.state.evaluation_headers.append(self.headers.get("X-Evaluation-Context"))
            self.state.cookie_headers.append(self.headers.get("Cookie"))
        authorization = self.headers.get("Authorization", "")
        token = authorization[7:] if authorization.startswith("Bearer ") else ""
        if token not in self.state.tokens:
            return self._json(401, {"error": "Missing or invalid Authorization header"})
        if self.state.reject_next_chat:
            self.state.reject_next_chat = False
            self.state.tokens.pop(token, None)
            # Consume the body so a kept-alive connection stays aligned, and do
            # not record the message: the website rejects auth before handling it.
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            return self._json(401, {"error": "Token expired"})
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._json(400, {"error": "Invalid JSON body"})
        if not isinstance(data, dict) or not isinstance(data.get("message", ""), str):
            return self._json(400, {"error": "Message must be a string in a JSON object or form"})
        message = data.get("message", "").strip()
        if not message:
            return self._json(400, {"error": "Missing message"})
        session, created = self.state.session_for(_cookie(self.headers.get("Cookie", "")))
        session.messages.append(message)
        self._reply(session, created, message)

    def _reply(self, session: SessionState, created: bool, message: str) -> None:
        started = time.monotonic()
        self.state.enter()
        try:
            directive = _directive(self.state, message)
            if directive == "hang":
                time.sleep(self.state.hang_seconds)
            elif directive.startswith("slow:"):
                time.sleep(float(directive[5:]))
            if directive == "disconnect":
                self.close_connection = True
                return
            if directive == "status-500":
                return self._json(500, {"error": "Internal Server Error"})
            cookie = session.session_id if created else None
            self._json(200, {"response": f"echo: {message}", "basket": []}, cookie)
        finally:
            self.state.leave(RequestSpan(session.session_id, message, started, time.monotonic()))


def _cookie(header: str) -> str | None:
    match = re.search(r"sessionid=([A-Za-z0-9]+)", header)
    return match.group(1) if match else None


def _directive(state: ChatServerState, message: str) -> str:
    match = re.search(r"\[([a-z0-9:.-]+)\]", message)
    if not match:
        return ""
    directive = match.group(1)
    if directive == "hang-once":
        with state.lock:
            if message in state.hung_once:
                return ""
            state.hung_once.add(message)
        return "hang"
    return directive


class ChatServer:
    """Threaded server bound to loopback on an ephemeral port."""

    def __init__(self) -> None:
        self.state = ChatServerState()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._server.block_on_close = False
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "ChatServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
