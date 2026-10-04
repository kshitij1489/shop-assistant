"""Shared Nominatim pace limit.

The public endpoint allows one request per second for the whole application.
Web workers and background jobs share exclusive ownership in Redis, followed
by a cooldown after completion. A lookup that cannot acquire ownership is not
sent. Redis must be shared, persistent, and configured without key eviction.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import math
import threading
import time
from typing import Optional
from urllib.parse import urlsplit
from uuid import uuid4

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)
PUBLIC_HOST = "nominatim.openstreetmap.org"
PUBLIC_INTERVAL_MS = 1000
_KEY_PREFIX = "geocoding:nominatim"
_client = None
_client_lock = threading.Lock()

# Ownership covers the HTTP call, including delays after Redis replies. It must
# not expire: a paused owner could otherwise resume and send alongside its successor.
# A crashed owner therefore fails closed until an operator recovers it (see docs).
_ACQUIRE = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return -1
end
local remaining = redis.call('PTTL', KEYS[2])
if remaining == -1 then
  return -1
end
if remaining >= 0 then
  return math.max(1, remaining)
end
redis.call('SET', KEYS[1], ARGV[1])
return 0
"""

# Install the full cooldown before releasing ownership, atomically. Its expiry is
# the deadline itself; a longer existing cooldown is never shortened.
_FINISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return -1
end
local remaining = redis.call('PTTL', KEYS[2])
if remaining ~= -1 then
  local delay = math.max(tonumber(ARGV[2]), remaining)
  redis.call('SET', KEYS[2], 'cooldown', 'PX', delay)
end
redis.call('DEL', KEYS[1])
return 0
"""


@dataclass
class Dispatch:
    cooldown_ms: int

    def rate_limited(self, retry_after: Optional[str], response_date: Optional[str] = None) -> None:
        self.cooldown_ms = max(self.cooldown_ms, _retry_after_ms(retry_after, response_date))


@contextmanager
def dispatch(base_url: str):
    """Serialize an HTTP request, then impose a gap after it completes."""
    interval_ms = interval_ms_for(base_url)
    permit = Dispatch(interval_ms)
    if interval_ms <= 0:
        yield permit
        return
    token = uuid4().hex
    if not _acquire(base_url, token):
        yield None
        return
    try:
        yield permit
    finally:
        if _command(_FINISH, base_url, token, permit.cooldown_ms) != 0:
            logger.error("OpenStreetMap dispatch release failed; limiter remains closed until recovery")


def _acquire(base_url: str, token: str) -> bool:
    deadline = time.monotonic() + _max_wait_ms() / 1000
    while True:
        wait_ms = _command(_ACQUIRE, base_url, token)
        if wait_ms is None:
            return False
        if wait_ms == 0:
            return True
        remaining = deadline - time.monotonic()
        # Busy owners are polled; cooldowns sleep until their deadline, then
        # recheck atomically. There are no advance reservations to go stale.
        delay = 0.05 if wait_ms < 0 else wait_ms / 1000
        if remaining <= 0 or (wait_ms > 0 and delay > remaining):
            logger.warning("OpenStreetMap lookup skipped to stay within the request limit")
            return False
        time.sleep(min(delay, remaining))
        if time.monotonic() > deadline:
            logger.warning("OpenStreetMap lookup skipped to stay within the request limit")
            return False


def _retry_after_ms(value: Optional[str], response_date: Optional[str] = None) -> int:
    # RFC 9110 permits delay-seconds or an HTTP-date. Use the server's Date for
    # relative dates when available so local clock skew cannot shorten the wait.
    if isinstance(value, str):
        value = value.strip()
        if value.isascii() and value.isdigit():
            try:
                return max(1000, int(value) * 1000)
            except ValueError:
                pass
        else:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    raise ValueError("Retry-After must include a timezone")
                now = datetime.now(timezone.utc)
                if isinstance(response_date, str):
                    try:
                        server_now = parsedate_to_datetime(response_date)
                        if server_now.tzinfo is not None:
                            now = server_now
                    except (TypeError, ValueError, OverflowError):
                        pass
                return max(1000, math.ceil((retry_at - now).total_seconds() * 1000))
            except (TypeError, ValueError, OverflowError):
                pass
    return 60_000


def interval_ms_for(base_url: str) -> int:
    """Public Nominatim is never faster than one request per second."""
    host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    floor = PUBLIC_INTERVAL_MS if host == PUBLIC_HOST else 0
    configured = _configured_interval_ms()
    if configured is None:
        return floor
    return max(configured, floor)


def _configured_interval_ms() -> Optional[int]:
    raw = getattr(settings, "NOMINATIM_MIN_INTERVAL_SECONDS", None)
    if raw is None or raw == "":
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured("NOMINATIM_MIN_INTERVAL_SECONDS must be zero or positive.") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise ImproperlyConfigured("NOMINATIM_MIN_INTERVAL_SECONDS must be zero or positive.")
    return math.ceil(seconds * 1000)


def _max_wait_ms() -> int:
    raw = getattr(settings, "NOMINATIM_MAX_QUEUE_SECONDS", 1)
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured("NOMINATIM_MAX_QUEUE_SECONDS must be zero or positive.") from exc
    if not math.isfinite(seconds) or seconds < 0:
        raise ImproperlyConfigured("NOMINATIM_MAX_QUEUE_SECONDS must be zero or positive.")
    return int(seconds * 1000)


def _command(script: str, base_url: str, *args) -> Optional[int]:
    try:
        import redis.exceptions
        key = _key(base_url)
        result = _redis().eval(script, 2, key + ":owner", key + ":cooldown", *args)
        return int(result)
    except ImportError as exc:
        _log_unavailable(exc)
        return None
    except (redis.exceptions.RedisError, OSError, ValueError, TypeError) as exc:
        _log_unavailable(exc)
        return None


def _log_unavailable(exc: BaseException) -> None:
    logger.warning("OpenStreetMap rate limiter unavailable", extra={"error": type(exc).__name__})


def _key(base_url: str) -> str:
    host = (urlsplit(base_url).hostname or "nominatim").lower().rstrip(".")
    return f"{_KEY_PREFIX}:{host}"


def _redis():
    global _client
    import redis
    with _client_lock:
        if _client is None:
            url = str(getattr(settings, "APP_REDIS_URL", "") or getattr(settings, "CELERY_BROKER_URL", "") or "")
            if not url.strip():
                raise redis.RedisError("Redis URL is not configured")
            _client = redis.Redis.from_url(
                url, socket_connect_timeout=0.3, socket_timeout=0.3, decode_responses=True,
            )
        return _client
