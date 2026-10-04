"""
Redis-backed session store for chatbot sessions.

Replaces in-memory MemorySessionStore with a Redis-backed implementation and
uses a Redis distributed lock around read-modify-write operations to avoid
race conditions across multiple Gunicorn workers and Celery processes.

Usage:
    from chatbot_core.logic.cafe.session.redis_session import RedisSessionStore
    session = RedisSessionStore(user_id, tenant_id=tenant.id, platform="telegram")

Notes:
- Requires `redis` Python package (redis-py).
- Configure settings.REDIS_URL (e.g., "redis://localhost:6379/0").
- TTL is applied to session key on writes to avoid unbounded growth.
- Conversation turns stage one snapshot under a renewable Redis lock.

"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event, Thread
import json
import logging
from typing import List, Optional, Tuple

from django.conf import settings
from django.utils.timezone import now
from redis import Redis

from chatbot_core.logic.cafe.session.base import BaseSessionStore
from chatbot_core.scope import required_identity, normalize_platform, session_identity
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.base import BaseIntent

logger = logging.getLogger(__name__)

# Configure Redis client
APP_SESSION_REDIS_URL = getattr(settings, "APP_SESSION_REDIS_URL", "redis://redis:6379/1")
_redis: Redis = Redis.from_url(APP_SESSION_REDIS_URL)
_turn_locks = ContextVar('cafe_redis_turn_locks', default={})

# Key patterns
KEY_PREFIX = "session:v2:"
LOCK_PREFIX = "lock:session:v2:"
DEFAULT_TTL_SECONDS = getattr(settings, "SESSION_TTL_SECONDS", 7 * 24 * 3600)  # 7 days


def _key(storage_id: str) -> str:
    return f"{KEY_PREFIX}{storage_id}"


def _lock_key(storage_id: str) -> str:
    return f"{LOCK_PREFIX}{storage_id}"


def _default_session() -> dict:
    return {
        "chat_history": [],
        "message_counter": 0,
        "basket": {},
        "delivery_address": {},
        "checklist": {"payment": False, "order": False, "location": False, "order_id": None},
        "ongoing_query_queue": [],
        "awaiting_followup_index": None,
        "last_activity_at": None,
    }


def _load(storage_id: str) -> dict:
    raw = _redis.get(_key(storage_id))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        logger.exception("Failed to decode session JSON for session %s", storage_id)
        return {}


class RedisSessionStore(BaseSessionStore):
    """Redis-backed implementation of BaseSessionStore.

    Uses a Redis distributed lock to protect read-modify-write operations.
    """

    def __init__(self, user_id: str, *, tenant_id, platform):
        self.user_id = required_identity(user_id, "user_id")
        self.tenant_id = required_identity(tenant_id, "tenant_id")
        self.platform = normalize_platform(platform)
        self._storage_id = session_identity(self.tenant_id, self.platform, self.user_id)
        # Never adopt legacy user-only keys. NX avoids overwriting a concurrent turn.
        _redis.set(_key(self._storage_id), json.dumps(_default_session()),
                   ex=DEFAULT_TTL_SECONDS or None, nx=True)

    def read_snapshot(self):
        return _load(self._storage_id) or _default_session()

    @contextmanager
    def turn_lock(self):
        # Renew while model/provider calls run; a crashed worker still expires.
        lock = _redis.lock(_lock_key(self._storage_id), timeout=60,
                           blocking_timeout=60, thread_local=False)
        if not lock.acquire(blocking=True):
            raise RuntimeError("Could not acquire conversation turn lock")
        stopped = Event()

        def renew():
            while not stopped.wait(20):
                try:
                    if not lock.extend(60, replace_ttl=True):
                        return
                except Exception:
                    logger.exception("Could not renew conversation turn lock")
                    return

        worker = Thread(target=renew, daemon=True)
        worker.start()
        token = _turn_locks.set({**_turn_locks.get(), id(self): lock})
        try:
            yield
        finally:
            stopped.set()
            worker.join(timeout=1)
            _turn_locks.reset(token)
            try:
                lock.release()
            except Exception:
                logger.exception("Failed to release conversation turn lock")

    def publish_snapshot(self, data):
        # Ownership check and publication are a single Redis operation. A
        # worker whose lease expired cannot overwrite a newer worker's turn.
        lock = _turn_locks.get()[id(self)]
        saved = _redis.eval("""
            if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
            redis.call('set', KEYS[2], ARGV[2])
            if tonumber(ARGV[3]) > 0 then redis.call('expire', KEYS[2], ARGV[3]) end
            return 1
        """, 2, _lock_key(self._storage_id), _key(self._storage_id),
            lock.local.token, json.dumps(data), DEFAULT_TTL_SECONDS or 0)
        if not saved:
            raise RuntimeError("Conversation turn lock was lost before saving")

    def _store(self) -> dict:
        staged = self.staged_snapshot()
        if staged is not None:
            return staged
        d = _load(self._storage_id)
        if not d:
            # ensure defaults exist
            d = _default_session()
        return d

    # --- read-only getters (no lock) ---
    def get_history(self) -> List[dict]:
        return self._store().get("chat_history", [])

    def get_counter(self) -> int:
        return int(self._store().get("message_counter", 0))

    def get_basket(self) -> Basket:
        return Basket.from_dict(self._store().get("basket", {}))

    def get_delivery_address(self) -> dict:
        return self._store().get("delivery_address", {})

    def get_checklist(self) -> dict:
        return self._store().get("checklist", {})

    def get_ongoing_queries(self) -> Tuple[List[BaseIntent], Optional[int]]:
        raw_queue = self._store().get("ongoing_query_queue", [])
        queue: List[BaseIntent] = []
        for i, q in enumerate(raw_queue):
            try:
                obj = BaseIntent.from_dict(q)
                queue.append(obj)
            except Exception as e:
                logger.exception("Failed to decode ongoing query #%d for user %s: %s", i, self.user_id, e)
                raise
        return queue, self._store().get("awaiting_followup_index")

    # Setters stage changes inside a turn; standalone setters use the same lock.
    def _update(self, **fields):
        with self.turn():
            self._store().update(fields, last_activity_at=now().isoformat())

    def set_history(self, history):
        self._update(chat_history=history[-200:])

    def increment_counter(self):
        with self.turn():
            counter = self.get_counter() + 1
            self._update(message_counter=counter)
            return counter

    def set_basket(self, basket):
        self._update(basket=basket.to_dict())

    def clear_basket(self):
        self._update(basket={})
        return self.get_basket()

    def set_delivery_address(self, delivery_address):
        self._update(delivery_address=delivery_address)

    def set_checklist(self, checklist):
        self._update(checklist=checklist)

    def clear_checklist(self):
        self._update(checklist=_default_session()['checklist'])
        return self.get_checklist()

    def set_ongoing_queries(self, queue, followup_index):
        self._update(ongoing_query_queue=[q.to_dict() for q in queue],
                     awaiting_followup_index=followup_index)
