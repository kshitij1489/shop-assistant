"""Authenticated, evaluation-only checks of the processes actually serving a run."""

import hashlib
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.http import JsonResponse

from evaluate.integration.location import evaluation_location_provider

PATH = '/__evaluation__/health'
SALT = 'evaluate.health.v1'


@lru_cache(maxsize=1)
def runtime_configuration():
    """Only allowlisted settings and code hashes; no credentials or environment dump."""
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for package in ('chatbot_core', 'commerce', 'orders', 'studio_desk', 'evaluate', 'mock_services'):
        for path in sorted((root / package).rglob('*.py')):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return {'models': {'chat': settings.LLM_MODEL, 'translate': settings.LLM_TRANSLATE_MODEL,
                       'analytics': settings.LLM_ANALYTICS_MODEL},
            'code_hash': digest.hexdigest(), 'database': settings.DATABASES['default']['NAME'],
            'evaluation_enabled': settings.EVALUATION_ENABLED,
            'location_provider': evaluation_location_provider()}


def shared_probe(nonce):
    from django.contrib.sessions.models import Session
    from django.core.cache import cache
    from redis import Redis
    root = Path(settings.EVALUATION_EVIDENCE_ROOT)
    session_cache = Redis.from_url(settings.APP_SESSION_REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    try:
        shared = (Session.objects.filter(session_key=nonce).exists()
                  and cache.get('eval-health:' + nonce) == nonce
                  and session_cache.get('eval-health:' + nonce) == nonce.encode()
                  and (root / ('.health-' + nonce)).is_file())
    finally:
        session_cache.close()
    return {**runtime_configuration(), 'shared_resources': shared}


class HealthMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path != PATH or not getattr(settings, 'EVALUATION_ENABLED', False):
            return self.get_response(request)
        try:
            data = signing.loads(request.headers.get('X-Evaluation-Health', ''), salt=SALT, max_age=60)
            nonce = data['nonce']
            if len(nonce) != 32 or any(c not in '0123456789abcdef' for c in nonce):
                raise ValueError('invalid nonce')
        except (signing.BadSignature, KeyError, ValueError, TypeError):
            return JsonResponse({'error': 'unauthorized'}, status=403)
        try:
            return JsonResponse(shared_probe(nonce))
        except Exception:
            return JsonResponse({'error': 'evaluation resources unavailable'}, status=503)
