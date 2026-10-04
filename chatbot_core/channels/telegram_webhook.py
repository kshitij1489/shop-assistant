import json, logging, requests
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from chatbot_core.tasks import enqueue_user_message
from chatbot_core.models import TenantInfo

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

def _extract_media(msg):
    if "video_note" in msg: return {"id": msg["video_note"]["file_id"], "type": "video_note"}
    if "video" in msg:      return {"id": msg["video"]["file_id"], "type": "video"}
    if "voice" in msg:      return {"id": msg["voice"]["file_id"], "type": "voice"}
    return None

def _try_send_error_message(bot_token: str | None, chat_id: int | None, text: str) -> bool:
    """
    Best-effort: try to tell the user something went wrong.
    Returns True if Telegram accepted the call (HTTP 200 + ok), else False.
    """
    if not bot_token or not chat_id:
        return False
    try:
        r = requests.post(
            TELEGRAM_API.format(token=bot_token),
            json={"chat_id": chat_id, "text": text},
            timeout=5,
        )
        ok = (r.status_code == 200 and r.json().get("ok") is True)
        if not ok:
            logger.warning("Failed to send error message to user: %s %s", r.status_code, r.text[:300])
        return ok
    except Exception as e:
        logger.exception("Exception while sending error message to user: %s", e)
        return False

@csrf_exempt
@require_POST
def telegram_webhook(request):
    """
    Always returns 200 to Telegram to avoid repeated retries.
    Attempts to inform the user in-chat when an error occurs (best-effort).
    """
    try:
        data = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        # No way to parse; cannot extract chat id. Just 200 and log.
        logger.error("Invalid JSON in Telegram webhook")
        return JsonResponse({"status": "ignored", "reason": "invalid_json"}, status=200)

    # Extract chat_id early so we can try messaging on any error.
    msg = data.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")

    bot_token = request.GET.get("token")
    if not bot_token:
        _try_send_error_message(None, chat_id, "Sorry, this bot isn’t configured yet. Please try again later.")
        return JsonResponse({"status": "ignored", "reason": "missing_token"}, status=200)

    try:
        tenant = TenantInfo.objects.get(telegram_bot_token=bot_token, is_active=True, approval_status="APPROVED")
    except TenantInfo.DoesNotExist:
        # Token doesn’t match any tenant we know; likely we also can’t send a message (token invalid or wrong bot).
        return JsonResponse({"status": "ignored", "reason": "invalid_bot_token"}, status=200)

    try:
        payload = {
            "channel": "telegram",
            "tenant_id": str(tenant.id),
            "user_id":   str((msg.get("from") or {}).get("id")),
            "chat_id":   chat_id,
            "bot_token": bot_token,
            "text":      msg.get("text", "") or "",
            "media":     _extract_media(msg),
            "meta":      {"update_id": data.get("update_id"), "received_at": now().isoformat()},
        }

        if not (payload["text"].strip() or payload["media"]):
            if "location" in msg or "venue" in msg:
                _try_send_error_message(
                    bot_token, chat_id,
                    "Please type your delivery address, including street address, city, state, country, and pincode.",
                )
                return JsonResponse({"status": "ignored", "reason": "unsupported_location"}, status=200)
            _try_send_error_message(bot_token, chat_id, "I received your message but couldn’t read any content.")
            return JsonResponse({"status": "ignored", "reason": "empty_message"}, status=200)
        enqueue_user_message(payload["tenant_id"], payload["user_id"], payload)
        # Optionally: immediate ACK to user for long processing
        # _try_send_error_message(bot_token, chat_id, "Thanks! Processing your request…")
        return JsonResponse({"status": "queued"}, status=200)

    except Exception as e:
        logger.exception("telegram_webhook failed: %s", e)
        _try_send_error_message(bot_token, chat_id,
            "Oops, something went wrong while handling your message. Please try again.")
        return JsonResponse({"status": "error_handled"}, status=200)
