"""Bounded acknowledged handoff. No SQL, retries or unacknowledged trimming here."""
from functools import lru_cache
from django.conf import settings
from redis import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

STREAM = 'conversation-recording:v1'
GROUP = 'postgres-writers-v1'


class EventTooLarge(ValueError):
    pass


@lru_cache(maxsize=8)
def _client(url, timeout):
    return Redis.from_url(url, socket_timeout=timeout, socket_connect_timeout=timeout,
                          retry=Retry(NoBackoff(), 0), decode_responses=True)


def client(*, writer=False):
    return _client(getattr(settings, 'CONVERSATION_RECORDING_REDIS_URL', 'redis://redis:6379/3'),
                   5.0 if writer else getattr(settings, 'CONVERSATION_RECORDING_TIMEOUT', 0.1))


def enqueue(encoded):
    if len(encoded.encode('utf-8')) > getattr(settings, 'CONVERSATION_RECORDING_MAX_EVENT_BYTES', 4 * 1024 * 1024):
        raise EventTooLarge('Recording event exceeds configured size limit')
    # No MAXLEN: dropping uncommitted entries would silently destroy evidence.
    client().xadd(STREAM, {'event': encoded})
