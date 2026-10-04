# chatbot_core/queues.py
from django.conf import settings
import redis

_r = None
def _redis():
    global _r
    if _r is None:
        _r = redis.Redis.from_url(getattr(settings, "APP_REDIS_URL", settings.CELERY_BROKER_URL), decode_responses=True)
    return _r

def enqueue_string(value: str, key: str | None = None, maxlen: int | None = None) -> None:
    r = _redis()
    k = key or getattr(settings, "STRING_QUEUE_KEY", "sd:string_queue:v1")
    n = maxlen or int(getattr(settings, "STRING_QUEUE_MAXLEN", 100000))
    p = r.pipeline()
    p.lpush(k, value)
    p.ltrim(k, 0, n - 1)
    p.execute()
