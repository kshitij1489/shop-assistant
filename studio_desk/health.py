"""Readiness for the application dependencies, without provider API calls."""
import logging

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
def health(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        cache.set('studio-desk:health', 'ok', timeout=30)
        if cache.get('studio-desk:health') != 'ok':
            raise RuntimeError('Cache unavailable')
    except Exception:
        logger.exception('Readiness check failed')
        return JsonResponse({'status': 'unavailable'}, status=503)
    return JsonResponse({'status': 'ok'})
