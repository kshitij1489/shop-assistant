"""Read-only guidance for requests that only store staff can fulfill."""
from django.db import DatabaseError

from chatbot_core.models import TenantInfo


SUPPORT_TOPICS = frozenset({
    'missing_or_wrong_items', 'delivery_problems',
    'refund_and_cancellation', 'address_or_contact_update',
})


def store_call_response(tenant=None, *, tenant_id=None):
    # Contact details may require a tenant lookup, but never an order lookup.
    if tenant is None and tenant_id is not None:
        try:
            tenant = TenantInfo.objects.filter(pk=tenant_id).first()
        except DatabaseError:
            tenant = None
    meta = getattr(tenant, 'meta', None)
    meta = meta if isinstance(meta, dict) else {}
    phone = getattr(tenant, 'support_phone', None) or meta.get('support_phone')
    email = getattr(tenant, 'support_email', None) or meta.get('support_email')
    contact = f" at {phone.strip()}" if isinstance(phone, str) and phone.strip() else ''
    email_contact = f"You can also email {email.strip()}. " if isinstance(email, str) and email.strip() else ''
    return (
        "Sorry about the trouble with your order. "
        f"Please call the store as soon as possible{contact}. "
        + email_contact +
        "The staff will check your order’s current status and confirm whether a refund, cancellation, "
        "replacement, or change is possible and advise on timing. "
        "Please share your order details directly with the store. "
        "I can’t edit or cancel placed orders, issue refunds or replacements, arrange callbacks, "
        "contact staff, or open complaint tickets through this chat. "
        "No action has been taken on your order through this chat."
    )
