from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from chatbot_core.models import TenantInfo
from chatbot_core.logic.cafe.db_utils import create_or_get_customer
from .utils import route_message_for_tenant
from chatbot_core.logic.cafe.session.redis_session import RedisSessionStore
import json
import logging

logger = logging.getLogger(__name__)

# Keep inbound processing unavailable until verification and outbound replies ship.
WHATSAPP_CHATBOT_AVAILABLE = False

@csrf_exempt
@require_POST
def whatsapp_webhook(request):
    import hashlib
    import hmac
    from django.conf import settings
    secret = getattr(settings, 'WHATSAPP_APP_SECRET', '')
    signature = request.headers.get('X-Hub-Signature-256', '')
    expected = 'sha256=' + hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    if not secret or len(signature) != len(expected) or not hmac.compare_digest(expected.encode(), signature.encode()):
        return JsonResponse({'error': 'Invalid webhook signature'}, status=403)
    if not WHATSAPP_CHATBOT_AVAILABLE:
        return JsonResponse({'error': 'WhatsApp chatbot is coming soon.'}, status=503)
    try:
        data = json.loads(request.body)
        phone_number_id = data["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"]
        incoming = data["entry"][0]["changes"][0]["value"]["messages"][0]
        message = incoming["text"]["body"]
        sender_id = incoming.get("from")

        if not phone_number_id or not message or not isinstance(sender_id, str) or not sender_id.strip():
            return JsonResponse({"error": "Missing WhatsApp data"}, status=400)

        tenant = TenantInfo.objects.get(whatsapp_id=phone_number_id, is_active=True, approval_status="APPROVED")
        customer = create_or_get_customer(tenant, platform="whatsapp", whatsapp_number=sender_id)
        reply = route_message_for_tenant(tenant, message, RedisSessionStore(
            sender_id,
            tenant_id=tenant.id, platform="whatsapp",
        ), request=request, customer=customer)

        logger.info(f"WhatsApp reply: {reply}")
        return JsonResponse({"status": "ok"})

    except TenantInfo.DoesNotExist:
        return JsonResponse({"error": "Invalid WhatsApp ID"}, status=404)
    except Exception as e:
        logger.exception("WhatsApp webhook error")
        return JsonResponse({"error": "Internal Server Error"}, status=500)
