"""Signed, owned HTTP correlation. Unsigned headers never activate controls."""
from functools import wraps
from django.core import signing
from django.http import JsonResponse
from evaluate.contracts.interfaces import Blocked
from .context import activate, enabled
from .ownership import resolve_ticket
from .telemetry import emit, span


def evaluation_request(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        value = request.META.get('HTTP_X_EVALUATION_CONTEXT') if enabled() else None
        if not value:
            return view(request, *args, **kwargs)
        try:
            ctx, owner = resolve_ticket(value)
            if request.session.session_key != owner['browser_sessions'][0]:
                raise Blocked('Browser session does not own evaluation context')
        except (signing.BadSignature, Blocked, ValueError, KeyError, TypeError):
            return JsonResponse({'error': 'Invalid evaluation context'}, status=403)
        with activate(ctx), span('http'):
            response = view(request, *args, **kwargs)
            emit('http.response', http_status=response.status_code)
            response['X-Evaluation-Request-ID'] = ctx.request_id
            return response
    return wrapped
