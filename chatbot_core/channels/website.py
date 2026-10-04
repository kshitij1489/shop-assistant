from django.http import JsonResponse
from django.db import transaction
from chatbot_core.channels.utils import generate_tenant_jwt, route_message_for_tenant
from chatbot_core.models import TenantInfo
from orders.models import Customer
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET
from chatbot_core.logic.cafe.session.django import DjangoSessionStore
from django.conf import settings
import json, logging, jwt
import hmac
from evaluate.controls.http import evaluation_request
from evaluate.controls.context import assert_scope
from chatbot_core.channels.streaming import stream_turn

logger = logging.getLogger(__name__)


def _website_customer(request, tenant, session_store):
    """Keep guest identity in the tenant's server-side browser session."""
    customer_id = session_store.session.get("customer_id")
    customer = Customer.objects.filter(tenant=tenant, pk=customer_id).first() if customer_id else None
    if customer is None:
        # Do not use the generic anonymous fallback: it can select another
        # visitor's customer record. An empty phone permits distinct guests.
        customer = Customer.objects.create(tenant=tenant, name="Guest", phone="")
        session_store.session["customer_id"] = str(customer.pk)
        request.session.modified = True
    return customer


@transaction.non_atomic_requests
@csrf_exempt
@require_POST
@evaluation_request
def chatbot_api(request):
    try:
        # Step 1: Extract & verify JWT
        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header.startswith("Bearer "):
            return JsonResponse({"error": "Missing or invalid Authorization header"}, status=401)

        token = auth_header[len("Bearer "):].strip()
        try:
            payload = jwt.decode(
                token,
                settings.JWT_SECRET,
                algorithms=["HS256"],
                options={"verify_exp": True},
                leeway=10  # 10 seconds buffer
            )
            tenant_slug = payload.get("tenant_slug")
        except jwt.ExpiredSignatureError:
            return JsonResponse({"error": "Token expired"}, status=401)
        except jwt.InvalidTokenError:
            return JsonResponse({"error": "Invalid token"}, status=401)

        if not isinstance(tenant_slug, str) or not tenant_slug.strip():
            return JsonResponse({"error": "Invalid token payload (no tenant_slug)"}, status=400)

        # Step 2: Fetch tenant
        try:
            tenant = TenantInfo.objects.get(slug=tenant_slug, is_active=True, approval_status='APPROVED')
        except TenantInfo.DoesNotExist:
            return JsonResponse({"error": f"Tenant '{tenant_slug}' not found"}, status=404)

        assert_scope(tenant.pk)

        # Step 3: Parse message
        if request.content_type == 'application/json':
            try:
                data = json.loads(request.body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return JsonResponse({"error": "Invalid JSON body"}, status=400)
        else:
            data = request.POST.dict()

        if not isinstance(data, dict) or not isinstance(data.get("message", ""), str):
            return JsonResponse({"error": "Message must be a string in a JSON object or form"}, status=400)
        message = data.get("message", "").strip()
        if not message:
            return JsonResponse({"error": "Missing message"}, status=400)

        # Step 4: Route to handler
        session_store = DjangoSessionStore(request, tenant_id=tenant.id)

        def process_turn():
            with session_store.turn():
                customer = _website_customer(request, tenant, session_store)
                assert_scope(tenant.pk, customer.pk)
                return route_message_for_tenant(
                    tenant, message, session_store, request=request, customer=customer,
                )

        if request.headers.get("Accept", "").split(",")[0].strip() == "text/event-stream":
            # Middleware runs before the body. It must set the session cookie,
            # but must not publish its stale snapshot outside the turn lock
            # (including with SESSION_SAVE_EVERY_REQUEST enabled).
            save = request.session.save
            request.session.save = lambda *args, **kwargs: None

            def streaming_turn():
                request.session.save = save
                return process_turn()

            return stream_turn(streaming_turn)

        response_text, basket = process_turn()

        return JsonResponse({"response": response_text, "basket": basket})

    except Exception:
        logger.exception("Unhandled error in chatbot_api")
        return JsonResponse({"error": "Internal Server Error"}, status=500)

@csrf_exempt
@require_GET
def public_jwt_token(request):
    tenant_slug = request.GET.get("tenant")
    if not tenant_slug:
        return JsonResponse({"error": "Missing tenant"}, status=400)

    api_key = request.headers.get("X-API-KEY")
    if not api_key:
        return JsonResponse({"error": "Missing API key"}, status=401)

    try:
        tenant = TenantInfo.objects.get(slug=tenant_slug, is_active=True, approval_status='APPROVED')
    except TenantInfo.DoesNotExist:
        return JsonResponse({"error": "Invalid tenant"}, status=404)

    expected, supplied = tenant.api_key.encode(), api_key.encode()
    if not tenant.api_key or len(expected) != len(supplied) or not hmac.compare_digest(expected, supplied):
        return JsonResponse({"error": "Invalid API key"}, status=403)

    token = generate_tenant_jwt(tenant_slug, expires_in_hours=1)
    return JsonResponse({"token": token})
