"""Lazy, bounded, immutable CPU indexes. PostgreSQL is the durable source.

No unscoped search or incremental add API: updates use the transactional cache
service. A DB revision fences snapshots across processes.
"""
from collections import Counter, OrderedDict
from dataclasses import dataclass
import logging
import threading

import numpy as np
from django.db import transaction
from django.utils import timezone

from chatbot_core.models import FaissVector, SemanticCacheState
from .config import policy

logger = logging.getLogger(__name__)


def unit_vector(value, dimension):
    vector = np.asarray(value, dtype=np.float32)
    if vector.shape != (dimension,) or not np.isfinite(vector).all():
        raise ValueError("Embedding has an invalid shape or non-finite values")
    wide = vector.astype(np.float64)
    norm = float(np.linalg.norm(wide))
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("Embedding has zero or invalid norm")
    return np.ascontiguousarray(wide / norm, dtype=np.float32)


def revision():
    return SemanticCacheState.objects.filter(pk=1).values_list("revision", flat=True).first() or 0


@dataclass(frozen=True)
class Snapshot:
    index: object
    ids: np.ndarray
    revision: int
    valid_until: object
    estimated_bytes: int


class ScopedIndexes:
    def __init__(self):
        # Serializes builds AND searches, limiting transient memory to one build.
        self._lock = threading.RLock()
        self._snapshots = OrderedDict()
        self._bytes = 0
        self._counters = Counter()

    def clear(self):
        with self._lock:
            self._snapshots.clear()
            self._bytes = 0

    def stats(self):
        with self._lock:
            return {**self._counters, "indexes": len(self._snapshots), "estimated_bytes": self._bytes}

    def _drop(self, key):
        old = self._snapshots.pop(key, None)
        if old is not None:
            self._bytes -= old.estimated_bytes

    def _room(self, needed, limits):
        while self._snapshots and (
            self._bytes + needed > limits.max_index_bytes or len(self._snapshots) >= limits.max_indexes
        ):
            self._drop(next(iter(self._snapshots)))
            self._counters["evictions"] += 1
        return needed <= limits.max_index_bytes

    def _build(self, partition, embedding_id, dimension, generation, limits):
        import faiss  # optional feature never imports native FAISS at app startup

        rows = FaissVector.objects.filter(
            cache_entry__partition=partition, cache_entry__embedding_id=embedding_id,
            cache_entry__expires_at__gt=timezone.now(), dim=dimension,
        ).order_by("-cache_entry__last_hit", "-cache_entry_id")
        count = min(rows.count(), limits.max_partition_rows)
        # Estimate includes native vectors, ID array and object overhead.
        needed = 4096 + count * (8 * dimension + 64) + min(count, 64) * (4 * dimension + 256)
        if not self._room(needed, limits):
            self._counters["oversized"] += 1
            return None
        index = faiss.IndexFlatIP(dimension)
        ids = np.empty(count, dtype=np.int64)
        deadline = None
        position = 0
        rows = rows.values_list("cache_entry_id", "vector", "cache_entry__expires_at")[:count]
        # Stream bytes without a full second vector matrix during construction.
        for pk, raw, expires_at in rows.iterator(chunk_size=64):
            try:
                if len(raw) != 4 * dimension:
                    raise ValueError("Invalid persisted vector byte length")
                vector = unit_vector(np.frombuffer(raw, dtype="<f4"), dimension)
            except ValueError:
                self._counters["invalid_vectors"] += 1
                logger.warning("Skipping invalid semantic vector entry_id=%s", pk)
                continue
            index.add(vector.reshape(1, -1))
            ids[position] = pk
            position += 1
            deadline = min(deadline, expires_at) if deadline else expires_at
        ids = ids[:position]
        ids.flags.writeable = False
        self._counters["builds"] += 1
        return Snapshot(index, ids, generation, deadline, needed)

    def search(self, vector, *, partition, embedding_id, dimension, k=5):
        if not partition or not embedding_id:
            raise ValueError("Scoped partition and embedding identity are required")
        limits = policy()
        if dimension != limits.dimension or embedding_id != limits.embedding_id:
            raise ValueError("Search embedding does not match configured encoder")
        key = (partition, embedding_id, dimension)
        query = unit_vector(vector, dimension).reshape(1, -1)
        k = min(max(int(k), 1), limits.max_partition_rows)
        # A caller's transaction can expose uncommitted vectors and a revision
        # that is reused after rollback. Search those rows only in a temporary
        # snapshot; never retain it for another request or transaction.
        retain = not transaction.get_connection().in_atomic_block
        with self._lock, transaction.atomic():
            while self._snapshots and (
                self._bytes > limits.max_index_bytes or len(self._snapshots) > limits.max_indexes
            ):
                self._drop(next(iter(self._snapshots)))
            generation = revision()
            snapshot = self._snapshots.get(key) if retain else None
            if not retain:
                self._drop(key)
            if snapshot and (snapshot.revision != generation or (
                snapshot.valid_until and snapshot.valid_until <= timezone.now()
            )):
                self._drop(key)
                snapshot = None
            if snapshot is None:
                # A write during a build cannot publish an older snapshot as current.
                for _ in range(2):
                    snapshot = self._build(partition, embedding_id, dimension, generation, limits)
                    if snapshot is None:
                        return [], []
                    latest = revision()
                    if latest == generation:
                        if retain:
                            self._snapshots[key] = snapshot
                            self._bytes += snapshot.estimated_bytes
                        break
                    generation = latest
                    snapshot = None
                if snapshot is None:
                    self._counters["revision_conflicts"] += 1
                    return [], []
            if retain:
                self._snapshots.move_to_end(key)
            if not snapshot.index.ntotal:
                return [], []
            distances, positions = snapshot.index.search(query, min(k, snapshot.index.ntotal))
            # Translate against the SAME immutable snapshot while holding the lock.
            matches = [(int(snapshot.ids[pos]), float(score))
                       for pos, score in zip(positions[0], distances[0]) if pos >= 0]
            self._counters["searches"] += 1
            return [row[0] for row in matches], [row[1] for row in matches]


indexes = ScopedIndexes()


def search(vector, **kwargs):
    return indexes.search(vector, **kwargs)


def rebuild_faiss_from_db():
    """Compatibility hook: invalidate locally; rebuild only demanded scopes."""
    indexes.clear()
