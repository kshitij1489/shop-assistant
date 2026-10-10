"""Best-effort answer cache with transactional admission and absolute expiry.

Only this module writes answer/vector pairs. The singleton DB lock enforces
global quotas across workers; Redis and process-local FAISS are disposable.
"""
from collections import Counter
from datetime import timedelta
import logging
import math
import re
import threading

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from chatbot_core.models import FaissVector, SemanticCacheEntry, SemanticCacheState
from chatbot_core.scope import required_identity, scope_digest
from evaluate.controls.cache import cache, namespace, semantic_lookup, semantic_write
from .config import policy
from .embedding_client import get_embedding
from .faiss_index import indexes, search, unit_vector

logger = logging.getLogger(__name__)
_metrics = Counter()
_metrics_lock = threading.Lock()


def record(event):
    with _metrics_lock:
        _metrics[event] += 1


def stats():
    with _metrics_lock:
        events = dict(_metrics)
    return {"events": events, "local_index": indexes.stats()}


def _normalize(text):
    # Keep accents, numbers and scripts; never translate or strip restrictions.
    return re.sub(r"\s+", " ", str(text).strip()).casefold()


def equivalent_queries(left, right):
    """Conservative semantic reuse: preserve every ordered word and number.

    Case, spacing, final sentence punctuation and an outer 'please' may differ.
    Broad paraphrases require a separately evaluated equivalence verifier; cosine
    similarity alone cannot establish interchangeable answers.
    """
    def tokens(value):
        result = re.findall(r"\w+(?:['’]\w+)*|[^\w\s]", _normalize(value), flags=re.UNICODE)
        while result and result[-1] in {".", "?", "!", "।", "。", "？", "！"}:
            result.pop()
        if result and result[0] == "please":
            result = result[1:]
        if result and result[-1] == "please":
            result = result[:-1]
        return result
    first, second = tokens(left), tokens(right)
    return bool(first) and first == second


def _hot_get(ctx, *, touch):
    try:
        value = cache.get(ctx["sig"])
        if isinstance(value, dict) and value.get("expires", 0) > timezone.now().timestamp():
            answer = value.get("response")
            if isinstance(answer, str) and answer.strip():
                if touch and value.get("entry_id"):
                    _touch(value["entry_id"], ctx["partition"])
                # A contended LRU update may finish after the answer expires.
                if value["expires"] > timezone.now().timestamp():
                    return answer
    except Exception:
        record("redis_read_error")
        logger.warning("Semantic exact-cache read failed", exc_info=True)


def _hot_set(ctx, answer, deadline):
    # Lookups can see rows written by their caller's uncommitted transaction.
    # Apply the same publication rule to every path, including exact-only mode.
    sig = ctx["sig"]
    value = {"response": answer, "expires": deadline.timestamp(), "entry_id": ctx.get("entry_id")}

    def publish():
        remaining = (deadline - timezone.now()).total_seconds()
        if remaining <= 0:
            return
        try:
            cache.set(sig, value, timeout=max(1, math.ceil(remaining)))
        except Exception:
            record("redis_write_error")
            logger.warning("Semantic exact-cache write failed", exc_info=True)

    if transaction.get_connection().in_atomic_block:
        transaction.on_commit(publish)
    else:
        publish()


def _touch(entry_id, partition):
    try:
        # An optional LRU update must not poison a surrounding transaction.
        with transaction.atomic():
            SemanticCacheEntry.objects.filter(pk=entry_id, partition=partition,
                expires_at__gt=timezone.now()).update(last_hit=timezone.now(), hit_count=F("hit_count") + 1)
    except Exception:
        record("touch_error")
        logger.warning("Semantic cache LRU update failed", exc_info=True)


@semantic_lookup("knowledge")
def lookup(model, scope, knowledge, question, ttl, *, system_id="cafebot-kb-answer-v4"):
    from chatbot_core.logic.cafe.prompts.utils import kb_fingerprint

    scope = namespace(required_identity(scope, "scope"))
    limits = policy()
    ttl = min(int(ttl), limits.max_ttl)
    norm_q = _normalize(question)
    kb_fp = kb_fingerprint(knowledge)
    partition = scope_digest(system_id, model, scope, kb_fp, limits.embedding_id)
    ctx = {"sig": scope_digest(partition, norm_q), "partition": partition,
           "scope": scope, "kb_fp": kb_fp, "norm_q": norm_q, "model": model,
           "system_id": system_id, "embedding_id": limits.embedding_id,
           "dimension": limits.dimension, "ttl": ttl}
    if ttl <= 0 or not norm_q or len(norm_q.encode()) > limits.max_query_bytes:
        return False, None, {}
    hot = _hot_get(ctx, touch=limits.enabled)
    if hot is not None:
        record("exact_hit")
        return True, hot, ctx
    if not limits.enabled:
        return False, None, ctx
    try:
        eligible = SemanticCacheEntry.objects.filter(
            partition=partition, embedding_id=limits.embedding_id, expires_at__gt=timezone.now(),
        )
        with transaction.atomic():
            entry = eligible.filter(sig=ctx["sig"]).first()
        if entry is not None and entry.expires_at > timezone.now():
            _touch(entry.pk, partition)
            if entry.expires_at > timezone.now():
                _hot_set({**ctx, "entry_id": entry.pk}, entry.response, entry.expires_at)
                record("durable_exact_hit")
                return True, entry.response, ctx
        ctx["qvec"] = unit_vector(get_embedding(norm_q), limits.dimension)
        ids, scores = search(ctx["qvec"], partition=partition, embedding_id=limits.embedding_id,
                             dimension=limits.dimension, k=32)
        # One scoped query, with a second deadline check before promotion.
        with transaction.atomic():
            candidates = {entry.pk: entry for entry in eligible.filter(pk__in=ids)}
        for pk, similarity in zip(ids, scores):
            entry = candidates.get(pk)
            if entry is None or similarity < limits.similarity or entry.expires_at <= timezone.now():
                continue
            if not equivalent_queries(norm_q, entry.normalized_query):
                record("equivalence_rejected")
                continue
            _touch(entry.pk, partition)
            if entry.expires_at <= timezone.now():
                continue
            # Reuse never extends the original absolute lifetime.
            _hot_set({**ctx, "entry_id": entry.pk}, entry.response, entry.expires_at)
            record("semantic_hit")
            return True, entry.response, ctx
    except Exception:
        # Broken cache infrastructure must not turn a generated answer into an error.
        record("lookup_error")
        logger.warning("Semantic lookup failed; generating fresh response", exc_info=True)
        ctx.pop("qvec", None)
        ctx["skip_durable"] = True
    record("miss")
    return False, None, ctx


def _state_lock():
    SemanticCacheState.objects.get_or_create(pk=1)
    return SemanticCacheState.objects.select_for_update().get(pk=1)


def _delete_batches(rows):
    deleted = 0
    while True:
        ids = list(rows.values_list("pk", flat=True)[:512])
        if not ids:
            return deleted
        SemanticCacheEntry.objects.filter(pk__in=ids).delete()
        deleted += len(ids)


def _trim(limits, partition=None, protected=None):
    rows = SemanticCacheEntry.objects.all()
    deleted = _delete_batches(rows.filter(Q(expires_at__lte=timezone.now()) | Q(partition="")))
    if partition:
        scoped = rows.filter(partition=partition).order_by("-last_hit", "-id")
        victims = list(scoped.values_list("pk", flat=True)[limits.max_partition_rows:])
        if victims:
            deleted += _delete_batches(rows.filter(pk__in=victims))
    # Evict least recently used entries until both logical storage budgets fit.
    totals = rows.aggregate(size=Sum("size_bytes"))
    count, size = rows.count(), totals["size"] or 0
    candidates = rows.exclude(pk=protected).order_by("last_hit", "id")
    while count > limits.max_rows or size > limits.max_db_bytes:
        victims = list(candidates.values_list("pk", "size_bytes")[:512])
        if not victims:
            break
        remove = []
        for pk, row_size in victims:
            if count <= limits.max_rows and size <= limits.max_db_bytes:
                break
            remove.append(pk)
            count -= 1
            size -= row_size
        deleted += _delete_batches(rows.filter(pk__in=remove))
    if deleted:
        record("pruned")
    return deleted


@semantic_write
def store(ctx, response, ttl, model):
    if not ctx or not isinstance(response, str) or not response.strip():
        return response
    limits = policy()
    ttl = min(int(ttl), ctx.get("ttl", 0), limits.max_ttl)
    if ttl <= 0 or len(response.encode()) > limits.max_response_bytes:
        return response
    deadline = timezone.now() + timedelta(seconds=ttl)
    if not limits.enabled or ctx.get("skip_durable"):
        _hot_set(ctx, response, deadline)
        return response
    try:
        if model != ctx["model"] or ctx["embedding_id"] != limits.embedding_id:
            return response
        vector = ctx.get("qvec")
        if vector is None:
            vector = get_embedding(ctx["norm_q"])
        vector = unit_vector(vector, limits.dimension).astype("<f4")
        size = vector.nbytes + len(response.encode()) + len(ctx["norm_q"].encode()) + 1024
        if size > limits.max_db_bytes:
            record("admission_rejected")
            return response
        with transaction.atomic():
            state = _state_lock()
            # Concurrent upserts are safe because admissions share this lock
            # and the DB also enforces partition/signature uniqueness.
            entry, _ = SemanticCacheEntry.objects.update_or_create(
                partition=ctx["partition"], sig=ctx["sig"],
                defaults={"scope": ctx["scope"], "kb_fp": ctx["kb_fp"],
                          "normalized_query": ctx["norm_q"], "response": response,
                          "system_id": ctx["system_id"], "model": model,
                          "embedding_id": limits.embedding_id, "expires_at": deadline,
                          "size_bytes": size, "last_hit": timezone.now()},
            )
            # Repairs a missing vector too; failure rolls back BOTH rows and revision.
            FaissVector.objects.update_or_create(cache_entry=entry,
                defaults={"dim": limits.dimension, "vector": vector.tobytes()})
            _trim(limits, ctx["partition"], protected=entry.pk)
            state.revision += 1
            state.save(update_fields=["revision"])
            hot_ctx = {**ctx, "entry_id": entry.pk}
            _hot_set(hot_ctx, response, deadline)
            transaction.on_commit(lambda: record("stored"))
    except Exception:
        record("store_error")
        logger.warning("Semantic cache admission failed; returning generated response", exc_info=True)
    return response


def prune(*, invalidate=False):
    """Scheduled retention and global invalidation, visible to every worker."""
    with transaction.atomic():
        state = _state_lock()
        limits = policy()
        deleted = _trim(limits)
        # Also enforce a lowered partition cap for scopes that receive no writes.
        oversized = SemanticCacheEntry.objects.values("partition").annotate(total=Count("id")).filter(
            total__gt=limits.max_partition_rows,
        )
        for partition in list(oversized.values_list("partition", flat=True)):
            rows = SemanticCacheEntry.objects.filter(partition=partition).order_by("-last_hit", "-id")
            victims = list(rows.values_list("pk", flat=True)[limits.max_partition_rows:])
            deleted += _delete_batches(SemanticCacheEntry.objects.filter(pk__in=victims))
        if deleted or invalidate:
            state.revision += 1
            state.save(update_fields=["revision"])
    return deleted
