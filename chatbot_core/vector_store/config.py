"""Validated resource limits and immutable embedding identity; no model loads."""
from dataclasses import dataclass
import hashlib
import json
import re

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_REVISION = "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"


@dataclass(frozen=True)
class CachePolicy:
    enabled: bool
    model: str
    revision: str
    dimension: int
    max_rows: int
    max_partition_rows: int
    max_db_bytes: int
    max_index_bytes: int
    max_indexes: int
    max_ttl: int
    max_query_bytes: int
    max_response_bytes: int
    similarity: float

    @property
    def embedding_id(self):
        spec = [self.model, self.revision, self.dimension, "float32-l2-v1"]
        return hashlib.sha256(json.dumps(spec).encode()).hexdigest()


def policy():
    def positive(name, default):
        value = getattr(settings, name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ImproperlyConfigured(f"{name} must be a positive integer")
        return value

    model = getattr(settings, "EMBEDDING_MODEL", DEFAULT_MODEL)
    revision = getattr(settings, "EMBEDDING_REVISION", DEFAULT_REVISION if model == DEFAULT_MODEL else "")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ImproperlyConfigured("EMBEDDING_REVISION must pin a 40-character model commit")
    dimension = positive("EMBEDDING_DIMENSION", 384)
    if dimension > 4096:
        raise ImproperlyConfigured("EMBEDDING_DIMENSION must not exceed 4096")
    similarity = float(getattr(settings, "SEMANTIC_CACHE_SIMILARITY", 0.98))
    if not 0 < similarity <= 1:
        raise ImproperlyConfigured("SEMANTIC_CACHE_SIMILARITY must be in (0, 1]")
    return CachePolicy(
        enabled=getattr(settings, "SEMANTIC_CACHE_ENABLED", False),
        model=model, revision=revision, dimension=dimension,
        max_rows=positive("SEMANTIC_CACHE_MAX_ROWS", 10000),
        max_partition_rows=positive("SEMANTIC_CACHE_MAX_PARTITION_ROWS", 512),
        max_db_bytes=positive("SEMANTIC_CACHE_MAX_DB_BYTES", 64 * 1024 * 1024),
        max_index_bytes=positive("SEMANTIC_CACHE_MAX_INDEX_BYTES", 32 * 1024 * 1024),
        max_indexes=positive("SEMANTIC_CACHE_MAX_INDEXES", 64),
        max_ttl=positive("SEMANTIC_CACHE_MAX_TTL", 1800),
        max_query_bytes=positive("SEMANTIC_CACHE_MAX_QUERY_BYTES", 4096),
        max_response_bytes=positive("SEMANTIC_CACHE_MAX_RESPONSE_BYTES", 16384),
        similarity=similarity,
    )
