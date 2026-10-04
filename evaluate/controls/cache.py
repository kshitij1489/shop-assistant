"""Isolate response caches without clearing Redis, DB rows or the FAISS index."""
from functools import wraps
from datetime import timezone
from hashlib import sha256
from django.core.cache import cache as django_cache
from .context import current
from .telemetry import emit


def namespace(value):
    ctx = current()
    if ctx is None:
        return value
    return sha256(('evaluation:v1:' + ctx.lease_id + ':' + ctx.business_at.astimezone(timezone.utc).isoformat()
                   + ':' + str(value)).encode()).hexdigest()


class ResponseCache:
    def get(self, key, default=None, **kwargs):
        ctx = current()
        if ctx is None:
            return django_cache.get(key, default, **kwargs)
        if ctx.cache_mode == 'cold':
            emit('cache.lookup', cache='response', tier='exact', hit=False, status='bypassed')
            return default
        value = django_cache.get(namespace(key), default, **kwargs)
        emit('cache.lookup', cache='response', tier='exact', hit=value is not default)
        return value

    def set(self, key, value, *args, **kwargs):
        ctx = current()
        if ctx and ctx.cache_mode == 'cold':
            return None
        return django_cache.set(namespace(key), value, *args, **kwargs)


cache = ResponseCache()


def semantic_lookup(name):
    """Both semantic lookup APIs return (hit, value, preparation_context)."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            ctx = current()
            if ctx and ctx.cache_mode == 'cold':
                emit('cache.lookup', cache=name, tier='semantic', hit=False, status='bypassed')
                return False, None, {}
            result = fn(*args, **kwargs)
            if ctx:
                emit('cache.lookup', cache=name, tier='combined', hit=result[0])
            return result
        return wrapped
    return decorate


def semantic_write(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        active = current()
        if active and active.cache_mode == 'cold':
            return args[1] if len(args) > 1 else kwargs.get('response', kwargs.get('result_tuple'))
        return fn(*args, **kwargs)
    return wrapped
