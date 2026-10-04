"""Website chat transport: tenant token flow plus one isolated cookie jar per lease.

Uses only the standard library. Sends exactly one POST per `send`; a timed-out or
reset request is reported as ambiguous and is never replayed here.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import http.client
import http.cookiejar
import json
import re
import socket
import threading
import time
from typing import Literal
from urllib.parse import urlsplit
import urllib.error
import urllib.request

from evaluate.contracts.interfaces import Blocked, ChatRequest, ChatResponse, Lease
from evaluate.runner.ports import CredentialResolver

SessionCookie = tuple[str, Callable[[Lease], str]]
EvaluationHeader = tuple[str, Callable[[Lease, ChatRequest], str]]
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]+")

TOKEN_PATH = "/agent_core/token/"
CHAT_PATH = "/agent_core/chatbot-api/"
MAX_BODY_BYTES = 1 << 20
DispatchState = Literal["not_dispatched", "in_flight_unknown", "completed"]


@dataclass(frozen=True)
class WebsiteChatResponse(ChatResponse):
    """Adds the server's sanitized error text and a dispatch classification."""
    error_text: str | None = None
    basket_size: int | None = None
    dispatch_state: DispatchState = "completed"
    sent_message: str | None = None


@dataclass(repr=False)
class _Session:
    """Private per-lease transport state; never serialized."""
    jar: http.cookiejar.CookieJar
    opener: urllib.request.OpenerDirector
    token: str | None = None
    token_obtained_at: float = 0.0
    cookie_name: str | None = None
    cookie_value: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __repr__(self) -> str:
        return "_Session(<redacted>)"


class TokenUnavailable(OSError):
    """The tenant-token GET failed before any chat POST. Nothing was dispatched."""


def classify_failure(exc: BaseException) -> tuple[DispatchState, str]:
    """Map a transport exception to a dispatch state and a `transport_error` label.

    Connection refusal and name resolution happen before any byte is sent, so
    those are `not_dispatched`. Timeouts and resets cannot be distinguished
    between connect and read phases through urllib, so they are ambiguous.
    """
    reason = getattr(exc, "reason", exc)
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return "not_dispatched", "connection"
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "in_flight_unknown", "timeout"
    return "in_flight_unknown", "connection"


class WebsiteTransport:
    """`HTTPTransport` implementation for the public website chat API."""

    def __init__(self, base_url: str, credentials: CredentialResolver, timeout_seconds: float,
                 token_max_age_seconds: float = 3000.0, *,
                 session_cookie: SessionCookie | None = None,
                 evaluation_header: EvaluationHeader | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._credentials = credentials
        self.timeout_seconds = timeout_seconds
        self.token_max_age_seconds = token_max_age_seconds
        self._session_cookie = session_cookie
        self._evaluation_header = evaluation_header
        self._sessions: dict[str, _Session] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"WebsiteTransport(base_url={self.base_url!r})"

    def _session(self, lease: Lease) -> _Session:
        with self._lock:
            session = self._sessions.get(lease.handle)
            if session is None:
                jar = http.cookiejar.CookieJar()
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
                session = self._sessions[lease.handle] = _Session(jar=jar, opener=opener)
            return session

    def authenticate(self, lease: Lease) -> None:
        """Fetch a tenant JWT with the private API key; safe to repeat (GET)."""
        session = self._session(lease)
        credential = self._credentials.resolve(lease)
        request = urllib.request.Request(
            f"{self.base_url}{TOKEN_PATH}?tenant={urllib.request.quote(credential.tenant_slug)}",
            headers={"X-API-KEY": credential.api_key, "Accept": "application/json"}, method="GET",
        )
        try:
            with session.opener.open(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read(MAX_BODY_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise Blocked(f"website token request rejected with HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise TokenUnavailable(type(exc).__name__) from None
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise Blocked("website token response lacked a token")
        session.token, session.token_obtained_at = token, time.monotonic()

    def prepare(self, lease: Lease) -> None:
        """Ensure a usable token. A young token does not cause another token request."""
        session = self._session(lease)
        with session.lock:
            self._install_cookie(lease, session)
            self._ensure_token(lease, session)

    def _ensure_token(self, lease: Lease, session: _Session) -> str:
        if session.token is None or time.monotonic() - session.token_obtained_at > self.token_max_age_seconds:
            self.authenticate(lease)
        assert session.token is not None
        return session.token

    def send(self, lease: Lease, request: ChatRequest) -> WebsiteChatResponse:
        session = self._session(lease)
        with session.lock:  # one in-flight request per identity, always sequential
            self._install_cookie(lease, session)
            # A context failure happens before the chat POST, so nothing was dispatched.
            header = self._context_header(lease, request)
            try:
                token = self._ensure_token(lease, session)
            except TokenUnavailable as exc:
                return WebsiteChatResponse(status_code=None, response_text=None, elapsed_ms=0.0,
                                           transport_error="connection", error_text=str(exc),
                                           dispatch_state="not_dispatched")
            started = time.perf_counter()
            response = self._post(session, token, request.turn.text, started, header)
            if response.status_code != 401:
                return response
            # The website rejects the token before it reads the message, so one
            # fresh token and one new POST cannot duplicate a committed turn.
            session.token = None
            try:
                token = self._ensure_token(lease, session)
            except (Blocked, TokenUnavailable):
                return response
            return self._post(session, token, request.turn.text, started, header)

    def _install_cookie(self, lease: Lease, session: _Session) -> None:
        if self._session_cookie is None:
            return
        name, value_of = self._session_cookie
        value = value_of(lease)
        if not _safe_cookie(name, value):
            raise Blocked("Owned browser cookie is not a single cookie token")
        if session.cookie_name == name and session.cookie_value == value:
            return
        _plant_cookie(session.jar, self.base_url, name, value)
        session.cookie_name, session.cookie_value = name, value

    def _context_header(self, lease: Lease, request: ChatRequest) -> tuple[str, str] | None:
        if self._evaluation_header is None:
            return None
        name, value_of = self._evaluation_header
        value = value_of(lease, request)
        if not _HEADER_NAME.fullmatch(name) or not _safe_header(value):
            raise Blocked("Evaluation context header was rejected")
        return name, value

    def _post(self, session: _Session, token: str, message: str, started: float,
              header: tuple[str, str] | None) -> WebsiteChatResponse:
        body = json.dumps({"message": message}).encode("utf-8")  # blank probes stay blank
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                   "Accept": "application/json"}
        if header is not None:
            headers[header[0]] = header[1]
        http_request = urllib.request.Request(
            f"{self.base_url}{CHAT_PATH}", data=body, method="POST", headers=headers,
        )
        try:
            with session.opener.open(http_request, timeout=self.timeout_seconds) as response:
                return _parse(response.status, response.read(MAX_BODY_BYTES), started)
        except urllib.error.HTTPError as exc:
            return _parse(exc.code, exc.read(MAX_BODY_BYTES), started)
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            state, label = classify_failure(exc)
            return WebsiteChatResponse(status_code=None, response_text=None, transport_error=label,
                                       elapsed_ms=_elapsed(started), dispatch_state=state)

    def close(self, lease: Lease) -> None:
        with self._lock:
            session = self._sessions.pop(lease.handle, None)
        if session is not None:
            session.jar.clear()
            session.token = None


def _safe_cookie(name: str, value: str) -> bool:
    return bool(_HEADER_NAME.fullmatch(name)) and bool(value) and len(value) <= 4096 and not any(
        char in value for char in "\r\n;, ")


def _safe_header(value: str) -> bool:
    return bool(value) and len(value) <= 8192 and "\r" not in value and "\n" not in value


def _plant_cookie(jar: http.cookiejar.CookieJar, base_url: str, name: str, value: str) -> None:
    parts = urlsplit(base_url)
    if not parts.hostname:
        raise Blocked("Website origin has no host")
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=parts.hostname, domain_specified=False, domain_initial_dot=False,
        path="/", path_specified=True, secure=parts.scheme == "https",
        expires=None, discard=True, comment=None, comment_url=None, rest={"HttpOnly": None},
    ))


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _parse(status: int, raw: bytes, started: float) -> WebsiteChatResponse:
    elapsed = _elapsed(started)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return WebsiteChatResponse(status_code=status, response_text=None, elapsed_ms=elapsed,
                                   transport_error="invalid_response")
    if not isinstance(payload, dict):
        return WebsiteChatResponse(status_code=status, response_text=None, elapsed_ms=elapsed,
                                   transport_error="invalid_response")
    reply, error, basket = payload.get("response"), payload.get("error"), payload.get("basket")
    return WebsiteChatResponse(
        status_code=status, response_text=reply if isinstance(reply, str) else None, elapsed_ms=elapsed,
        error_text=error if isinstance(error, str) else None,
        basket_size=len(basket) if isinstance(basket, list) else None,
    )
