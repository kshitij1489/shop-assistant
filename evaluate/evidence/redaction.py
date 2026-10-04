"""Detect and mask credential material before anything reaches durable storage.

Typed schemas cannot recognize a JWT inside prose, so every free-form string and
every dictionary key is checked here. `assert_redacted` rejects; `redact` masks.
Synthetic scenario text (names, phones, addresses) is evidence and is kept;
only credential material is masked.
"""
from __future__ import annotations

import re
from typing import Any

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "bearer": re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "authorization_header": re.compile(r"(?i)\bauthorization\s*[:=]"),
    "cookie_header": re.compile(r"(?i)\b(set-cookie|cookie)\s*[:=]\s*[A-Za-z_][A-Za-z0-9_-]*="),
    "session_cookie": re.compile(r"(?i)\b(sessionid|csrftoken)\s*=\s*[A-Za-z0-9._-]{8,}"),
    "api_key_header": re.compile(r"(?i)\bx-api-key\b"),
    "api_key_assignment": re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd)\s*[:=]\s*\S{4,}"),
    "openai_key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
}

FORBIDDEN_KEYS = frozenset({
    "authorization", "cookie", "cookies", "set-cookie", "x-api-key", "api_key",
    "apikey", "password", "secret", "signing_secret", "token", "jwt", "headers",
})


class RedactionError(ValueError):
    """Artifact contains credential material; the diagnostic names only the pattern."""


def find_secrets(text: str) -> list[str]:
    """Return the names of secret patterns found in free-form text."""
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


def redact_text(text: str) -> str:
    """Mask every secret pattern match while keeping surrounding prose readable."""
    for name, pattern in SECRET_PATTERNS.items():
        text = pattern.sub(f"[REDACTED:{name}]", text)
    return text


def _walk_strings(value: Any, path: str):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                raise RedactionError(f"forbidden key at {path}/{key}")
            yield from _walk_strings(item, f"{path}/{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f"{path}/{index}")


def assert_redacted(value: Any) -> None:
    """Raise `RedactionError` when a JSON-like value carries credential material.

    Forbidden dictionary keys are rejected regardless of their value because a
    header or cookie container is never legitimate evidence.
    """
    for path, text in _walk_strings(value, ""):
        found = find_secrets(text)
        if found:
            raise RedactionError(f"secret pattern {found[0]} at {path or '/'}")


def redact_value(value: Any) -> Any:
    """Return a deep copy with secret text masked and forbidden keys removed.

    Use only for crash diagnostics that must be persisted even when unsafe input
    appears; typed evidence artifacts must use `assert_redacted` instead.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in value.items()
                if str(key).lower() not in FORBIDDEN_KEYS}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value
