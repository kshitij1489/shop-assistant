# chatbot_core/active_chats.py
import json
import time
from uuid import uuid4
from typing import Any, Dict, List, Optional

import redis
from django.conf import settings

# Reuse the same Redis URL convention as tasks.py
_r = redis.Redis.from_url(settings.CELERY_BROKER_URL)

# Key helpers
def _k_idx(tenant_id: str, channel: str) -> str:
    return f"acidx:{tenant_id}:{channel}"  # ZSET(chat_id -> last_activity_ts)

def _k_hash(tenant_id: str, channel: str, chat_id: str) -> str:
    return f"ac:{tenant_id}:{channel}:{chat_id}"  # HASH of chat summary

def _k_msgs(tenant_id: str, channel: str, chat_id: str) -> str:
    return f"msgs:{tenant_id}:{channel}:{chat_id}"  # LIST of compact JSON messages

def _k_global(tenant_id: str, channel: str) -> str:
    # tenant-wide, per-channel switch
    return f"acglobal:{tenant_id}:{channel}"


def delete_tenant_chat_data(tenant_id: int) -> int:
    """Remove tenant-scoped transcripts and summaries; propagate storage failures.

    Scan each namespace so orphaned transcripts without an active-chat index are
    also removed. The trailing colon prevents matching another tenant's ID.
    """
    tenant_id = int(tenant_id)
    with _r.pipeline(transaction=True) as pipeline:
        for namespace in ('msgs', 'ac', 'acidx', 'acglobal'):
            batch = []
            for key in _r.scan_iter(match=f'{namespace}:{tenant_id}:*', count=500):
                batch.append(key)
                if len(batch) == 500:
                    pipeline.delete(*batch)
                    batch = []
            if batch:
                pipeline.delete(*batch)
        # Scan failures leave Redis untouched; all queued removals execute in a
        # single Redis transaction, including more than one batch of keys.
        return sum(pipeline.execute())


def set_global_agent_enabled(tenant_id: str, channel: str, enabled: bool) -> None:
    # Operator controls must report persistence failures to the dashboard.
    _r.set(_k_global(tenant_id, channel), "1" if enabled else "0")

def is_global_agent_enabled(tenant_id: str, channel: str) -> bool:
    v = _r.get(_k_global(tenant_id, channel))
    if v is None:
        return True  # A missing setting defaults ON; an unavailable store raises.
    s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
    return s != "0"

# Public API
def touch_active_chat(tenant_id: str, channel: str, chat_id: str, *, 
                      user_id: Optional[str] = None, customer_id: Optional[str] = None, 
                      display_name: Optional[str] = None, phone: Optional[str] = None, 
                      last_text: Optional[str] = None, ts: Optional[int] = None) -> None:
    """Upsert a chat summary and bump the ZSET index by last activity timestamp."""
    ts = ts or int(time.time())
    key_h = _k_hash(tenant_id, channel, str(chat_id))
    key_z = _k_idx(tenant_id, channel)

    mapping: Dict[str, str] = {"last_activity_ts": str(ts)}
    if user_id is not None:
        mapping["user_id"] = str(user_id)
    if customer_id is not None:
        mapping["customer_id"] = str(customer_id)
    if display_name is not None:
        mapping["display_name"] = display_name
    if phone is not None:
        mapping["phone"] = phone
    if last_text is not None:
        mapping["last_text"] = last_text
    # agent_enabled defaults to "1" if missing; do not flip it here implicitly

    try:
        if mapping:
            _r.hset(key_h, mapping=mapping)
        _r.zadd(key_z, {str(chat_id): ts})
    except Exception:
        # Never raise to caller (dashboard is best-effort)
        pass


def list_active_chats(tenant_id: str, channel: str, *, limit: int = 50) -> List[Dict[str, Any]]:
    """Return recent chats (most recent first) with summary fields."""
    key_z = _k_idx(tenant_id, channel)
    try:
        chat_ids = _r.zrevrange(key_z, 0, max(0, limit - 1))
    except Exception:
        return []

    out: List[Dict[str, Any]] = []
    for cid_b in chat_ids:
        cid = cid_b.decode() if isinstance(cid_b, (bytes, bytearray)) else str(cid_b)
        key_h = _k_hash(tenant_id, channel, cid)
        try:
            h = _r.hgetall(key_h)
        except Exception:
            h = {}
        if not h:
            continue
        row = {k.decode(): v.decode() for k, v in h.items()}
        row["chat_id"] = cid
        # normalize some fields
        row["agent_enabled"] = row.get("agent_enabled", "1")
        row["last_activity_ts"] = int(row.get("last_activity_ts", "0"))
        out.append(row)
    return out


def append_message(tenant_id: str, channel: str, chat_id: str, *, direction: str, text: str, ts: Optional[int] = None, 
                   meta: Optional[Dict[str, Any]] = None, keep_last: int = 200) -> None:
    ts = ts or int(time.time())
    key_l = _k_msgs(tenant_id, channel, str(chat_id))

    payload = {
        "id": uuid4().hex,
        "dir": direction,
        "text": text or "",
        "ts": ts,
    }
    if meta:
        payload["meta"] = meta

    _r.rpush(key_l, json.dumps(payload, ensure_ascii=False))
    if keep_last > 0:
        _r.ltrim(key_l, -keep_last, -1)

    # Update chat summary
    try:
        touch_active_chat(
            tenant_id,
            channel,
            str(chat_id),
            last_text=text,
            ts=ts,
        )
    except Exception:
        pass


def get_messages(tenant_id: str, channel: str, chat_id: str, *, limit: int = 100) -> List[Dict[str, Any]]:
    key_l = _k_msgs(tenant_id, channel, str(chat_id))
    try:
        raw = _r.lrange(key_l, max(-limit, -1000), -1)
    except Exception:
        return []
    out: List[Dict[str, Any]] = []
    for b in raw:
        try:
            out.append(json.loads(b))
        except Exception:
            continue
    return out


def set_agent_enabled(tenant_id: str, channel: str, chat_id: str, enabled: bool) -> None:
    key_h = _k_hash(tenant_id, channel, str(chat_id))
    _r.hset(key_h, mapping={"agent_enabled": "1" if enabled else "0"})


def is_agent_enabled(tenant_id: str, channel: str, chat_id: str) -> bool:
    key_h = _k_hash(tenant_id, channel, str(chat_id))
    try:
        v = _r.hget(key_h, "agent_enabled")
        if v is None:
            return True  # default ON
        return (v.decode() if isinstance(v, (bytes, bytearray)) else str(v)) != "0"
    except Exception:
        return True

def set_latest_meta(tenant_id: str, channel: str, chat_id: str, meta: Optional[List[Dict[str, Any]]]) -> None:
    """Store an explicit basket snapshot, including []; None leaves it unchanged."""
    if not isinstance(meta, list):
        return
    key_h = _k_hash(tenant_id, channel, str(chat_id))
    try:
        _r.hset(key_h, mapping={"latest_meta": json.dumps(meta, ensure_ascii=False)})
    except Exception:
        pass


def get_latest_meta(tenant_id: str, channel: str, chat_id: str) -> Optional[List[Dict[str, Any]]]:
    """Retrieve the last stored basket for a chat, or None if missing / malformed."""
    key_h = _k_hash(tenant_id, channel, str(chat_id))
    try:
        v = _r.hget(key_h, "latest_meta")
        if not v:
            return None
        s = v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
        return json.loads(s)
    except Exception:
        return None
