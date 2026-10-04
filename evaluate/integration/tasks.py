"""Readiness probes run by the evaluation worker and beat; never call a model."""
from time import time
from celery import shared_task
from django.conf import settings
from django.core.cache import cache


@shared_task(name='evaluate.health')
def health(nonce):
    if not getattr(settings, 'EVALUATION_ENABLED', False):
        raise RuntimeError('Evaluation is disabled')
    from .health import shared_probe
    return shared_probe(nonce)


@shared_task(name='evaluate.beat_heartbeat')
def beat_heartbeat():
    if getattr(settings, 'EVALUATION_ENABLED', False):
        cache.set('evaluate:beat:heartbeat', time(), timeout=60)
