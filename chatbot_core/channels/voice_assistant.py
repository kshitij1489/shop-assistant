# chatbot_core/channels/voice_assistant.py
from __future__ import annotations

import json
import logging
import uuid
from django.utils.timezone import now
from django.http import JsonResponse, HttpResponseBadRequest
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from chatbot_core.tasks import enqueue_user_message

logger = logging.getLogger(__name__)

@csrf_protect
@require_POST
def voice_api(request):
    """
    Accepts JSON: {"tenant_id": "...", "chat_id": "...", "message": {"text": "..."}}
    Returns {"status": "queued"} (async processing).

    NOTE: We’re intentionally async to re-use the same pipeline (processor + Celery)
    as other channels. Frontend should poll your conversation/messages API to display
    the bot reply when it arrives.
    """
    from users.tenant_access import authenticated_tenant
    tenant = authenticated_tenant(request)
    ctype = request.META.get("CONTENT_TYPE", "")

    if "application/json" not in ctype:
        return HttpResponseBadRequest("Content-Type must be application/json")

    try:
        data = json.loads(request.body.decode("utf-8"))
    except Exception:
        return HttpResponseBadRequest("Invalid JSON body")

    if not isinstance(data, dict) or not isinstance(data.get("message"), dict) or not isinstance(data["message"].get("text"), str):
        return HttpResponseBadRequest("message.text is required")

    # Required bits
    msg = (data.get("message") or {})
    text = (msg.get("text") or "").strip()
    if not text:
        return HttpResponseBadRequest("message.text is required")

    if data.get('tenant_id') is not None and str(data['tenant_id']) != str(tenant.pk):
        return JsonResponse({'error': 'Tenant does not match authenticated account'}, status=403)
    # This assistant belongs to the authenticated dashboard browser session.
    session_key = f'va_chat_id:{tenant.pk}'
    chat_id = request.session.get(session_key)
    if not chat_id:
        chat_id = str(uuid.uuid4())
        request.session[session_key] = chat_id
    if data.get('chat_id') and str(data['chat_id']) != chat_id:
        return JsonResponse({'error': 'Chat does not match this session'}, status=403)
    user_id = chat_id

    payload = {
        "channel":   "voiceassistant",
        "tenant_id": str(tenant.id),
        "user_id":   user_id,
        "chat_id":   chat_id,
        "bot_token": None,           # not used for voiceassistant
        "text":      text,
        "ui_lang": (data.get("ui_lang") or "").strip(),
        "media":     None,           # text-only channel
        "meta":      {"received_at": now().isoformat()},
        # Optional UX fields used by processor for identity:
        "user_name": data.get("user_name"),
        "phone":     data.get("phone"),
    }

    # Enqueue for async processing
    enqueue_user_message(str(tenant.id), user_id, payload)
    return JsonResponse({"status": "queued", "chat_id": chat_id}, status=200)
