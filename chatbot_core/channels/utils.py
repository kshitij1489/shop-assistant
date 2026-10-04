import jwt
import datetime
from django.conf import settings
from chatbot_core.models import TenantInfo
from chatbot_core.logic.tenant_handlers.dispatcher import get_handler

def route_message_for_tenant(tenant: TenantInfo, message: str, session_store, request=None, customer=None):
    handler = get_handler(tenant, session_store, request=request)
    return handler.handle_message(message, customer)

def generate_tenant_jwt(tenant_slug: str, expires_in_hours: int = 12, extra_claims: dict = None) -> str:
    """
    Generates a JWT token for the given tenant.
    
    Args:
        tenant_slug (str): The unique slug identifying the tenant.
        expires_in_hours (int): Token validity period (default 12 hours).
        extra_claims (dict): Optional additional payload fields.

    Returns:
        str: Signed JWT token as string.
    """
    if not settings.JWT_SECRET:
        raise ValueError("JWT_SECRET not set in settings")

    payload = {
        "tenant_slug": tenant_slug,
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=expires_in_hours),
        "iat": datetime.datetime.utcnow()
    }

    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
    return token