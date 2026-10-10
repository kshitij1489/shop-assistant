from evaluate.controls.celery import EvaluationTask
import os
from celery import shared_task
import json, redis, logging
from django.conf import settings

logger = logging.getLogger(__name__)

def _r():
    return redis.Redis.from_url(getattr(settings, "APP_REDIS_URL", settings.CELERY_BROKER_URL))

from chatbot_core.scope import session_identity


def _qkey(tid, uid, platform): return "userq:v2:" + session_identity(tid, platform, uid)
def _lock(tid, uid, platform): return "lock:user:v2:" + session_identity(tid, platform, uid)


def validate_payload_scope(tenant_id, user_id, payload, platform=None):
    from chatbot_core.scope import normalize_platform
    channel = normalize_platform(payload.get('channel'))
    if str(payload.get('tenant_id')) != str(tenant_id) or str(payload.get('user_id')) != str(user_id):
        raise ValueError('Queued message identity does not match its owner.')
    if platform is not None and channel != normalize_platform(platform):
        raise ValueError('Queued message channel does not match its owner.')
    session_identity(tenant_id, channel, user_id)
    return channel

def enqueue_user_message(tenant_id, user_id, payload):
    platform = validate_payload_scope(tenant_id, user_id, payload)
    from evaluate.controls.context import current, assert_scope
    from evaluate.controls.ownership import worker_ticket, assert_session
    if current():
        assert_scope(tenant_id)
        assert_session(tenant_id, user_id, platform)
        payload = {**payload, '_evaluation_context': worker_ticket(current())}
    r = _r()
    r.rpush(_qkey(tenant_id, user_id, platform), json.dumps(payload))
    # If this is called inside a DB transaction, prefer on_commit(...)
    # from django.db import transaction
    # transaction.on_commit(lambda: drain_user_queue_task.delay(tenant_id, user_id))
    # Route to default so your worker definitely sees it.
    drain_user_queue_task.apply_async(args=[tenant_id, user_id, platform], queue="default", routing_key="def.task")

@shared_task(base=EvaluationTask, bind=True, acks_late=True, name="chatbot_core.drain_user_queue_task")
def drain_user_queue_task(self, tenant_id, user_id, platform=None):
    from evaluate.controls.context import assert_scope
    from evaluate.controls.ownership import assert_session
    assert_scope(tenant_id)
    assert_session(tenant_id, user_id, platform)
    if platform is None:
        raise ValueError("Legacy unscoped queue jobs must be replayed through their authenticated ingress.")
    r = _r()
    logger.info("Task Start: %s/%s", tenant_id, user_id)
    # Lazy import to avoid cycles
    from .processor import process_payload

    lock = r.lock(_lock(tenant_id, user_id, platform), timeout=30)
    if not lock.acquire(blocking=False):
        return
    try:
        while (item := r.lpop(_qkey(tenant_id, user_id, platform))):
            payload = json.loads(item)
            validate_payload_scope(tenant_id, user_id, payload, platform)
            from evaluate.controls.context import activate, enabled, assert_scope
            from evaluate.controls.ownership import resolve_ticket
            if enabled():
                value = payload.pop('_evaluation_context', None)
                context = resolve_ticket(value, worker=True)[0] if value else None
                with activate(context):
                    assert_scope(tenant_id)
                    assert_session(tenant_id, user_id, platform)
                    process_payload(tenant_id, user_id, payload)
            else:
                process_payload(tenant_id, user_id, payload)
    finally:
        if getattr(lock, "owned", None) and lock.owned():
            lock.release()

@shared_task(base=EvaluationTask, name="chatbot_core.embed_text", expires=15)
def embed_text(q: str) -> list[float]:
    # The first request also loads the model; use the normal task time limits.
    from chatbot_core.vector_store.embedding_client import get_embedding
    return get_embedding(q)

@shared_task(base=EvaluationTask, name="chatbot_core.rebuild_faiss")
def rebuild_faiss_task():
    # Version the durable source so ALL processes rebuild on their next lookup.
    from chatbot_core.vector_store.semantic_cache import prune
    return prune(invalidate=True)


@shared_task(base=EvaluationTask, name="chatbot_core.prune_semantic_cache")
def prune_semantic_cache_task():
    from chatbot_core.vector_store.semantic_cache import prune
    return prune()

def _r_string_queue():
    return redis.Redis.from_url(getattr(settings, "APP_REDIS_URL", settings.CELERY_BROKER_URL), decode_responses=True)

_STRING_QUEUE_MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB — rotate when exceeded
_STRING_QUEUE_BACKUP_COUNT = 3  # keep up to 3 rotated backups


def _rotate_if_needed(path: str) -> None:
    """Simple size-based log rotation: rename current -> .1, .1 -> .2, etc."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < _STRING_QUEUE_MAX_FILE_BYTES:
            return
    except OSError:
        return

    for i in range(_STRING_QUEUE_BACKUP_COUNT, 0, -1):
        src = f"{path}.{i}" if i > 1 else path
        dst = f"{path}.{i}"
        if i == _STRING_QUEUE_BACKUP_COUNT:
            # oldest backup — delete it
            try:
                os.remove(f"{path}.{i}")
            except FileNotFoundError:
                pass
        if i > 1:
            try:
                os.rename(f"{path}.{i - 1}", f"{path}.{i}")
            except FileNotFoundError:
                pass
    # rename current file to .1
    try:
        os.rename(path, f"{path}.1")
    except FileNotFoundError:
        pass


@shared_task(base=EvaluationTask, name="chatbot_core.drain_string_queue", acks_late=True)
def drain_string_queue():
    r = _r_string_queue()
    key = getattr(settings, "STRING_QUEUE_KEY", "sd:string_queue:v1")
    batch_size = int(getattr(settings, "STRING_QUEUE_BATCH_SIZE", 100))
    out_path = getattr(settings, "STRING_QUEUE_FILE", "/var/log/studio_desk/string_queue.log")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    popped = r.rpop(key, batch_size)
    if not popped:
        return 0
    if not isinstance(popped, list):
        popped = [popped]
    remaining = batch_size - len(popped)
    if remaining > 0:
        extra = r.rpop(key, remaining)
        if extra:
            popped.extend(extra if isinstance(extra, list) else [extra])

    _rotate_if_needed(out_path)

    with open(out_path, "a", encoding="utf-8") as f:
        for s in popped:
            f.write(s)
            f.write("\n")

    return len(popped)
