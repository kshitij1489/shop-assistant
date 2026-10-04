"""Validated identities for tenant-owned conversation state and caches."""

import hashlib
import json


def required_identity(value, name):
    if value is None or not str(value).strip():
        raise ValueError(f"{name} is required")
    return str(value)


def normalize_platform(platform):
    platform = required_identity(platform, "platform").strip().lower()
    return "website" if platform == "web" else platform


def scope_digest(*parts):
    """Unambiguous, bounded-length keys (also fit SemanticCacheEntry.scope)."""
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def session_identity(tenant_id, platform, user_id):
    parts = [required_identity(tenant_id, "tenant_id"), normalize_platform(platform),
             required_identity(user_id, "user_id")]
    return scope_digest(*parts)
