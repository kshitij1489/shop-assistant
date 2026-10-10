"""Delete unused tenant setup without erasing business or provider history."""
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction

from chatbot_core.active_chats import delete_tenant_chat_data
from chatbot_core.models import TenantInfo
from commerce.models import Configuration, Location
from orders.models import (
    AddonGroup, ChatSession, Customer, CustomerAddress, DeliveryPartner,
    ItemAddonGroup, MenuCategory, MenuItem, Order, PlatformWebhookLog, Tax,
)
from users.models import TenantProfile


BUSINESS_MODELS = (
    Order, Customer, ChatSession, CustomerAddress, MenuItem, MenuCategory,
    ItemAddonGroup, AddonGroup, Tax, DeliveryPartner, PlatformWebhookLog,
)


@transaction.atomic
def delete_unused_tenant(tenant_id: int, *, actor_id: int) -> tuple[int, int]:
    tenant = TenantInfo.objects.select_for_update().get(pk=tenant_id)
    # Holding the parent lock also serializes against committed FK inserts on
    # PostgreSQL. Check business data before removing any onboarding protection.
    if any(model.objects.filter(tenant_id=tenant.pk).exists() for model in BUSINESS_MODELS):
        raise ValidationError(
            'This tenant has customer, order, chat, menu or other business records. Deactivate it instead of deleting it.')

    profiles = list(TenantProfile.objects.select_for_update().filter(tenant=tenant))
    user_ids = [profile.user_id for profile in profiles]
    users = get_user_model().objects.select_for_update().filter(pk__in=user_ids)
    owners = list(users)
    if (any(profile.is_master for profile in profiles)
            or any(owner.is_superuser or owner.is_staff for owner in owners)
            or actor_id in user_ids):
        raise ValidationError('This tenant is linked to an operations account and cannot be deleted.')

    # Only disposable setup is removed explicitly. PROTECT on connections,
    # accepted orders and stock still aborts and rolls this entire operation back.
    locations = Location.objects.select_for_update().filter(tenant=tenant)
    list(locations)
    Configuration.objects.filter(tenant=tenant).delete()
    locations.delete()
    tenant.delete()
    users.delete()

    # Redis is not part of the SQL transaction. Perform cleanup synchronously
    # before committing or reporting success; an outage rolls SQL changes back.
    # Redis deletions already completed cannot be restored by a SQL rollback.
    deleted_chat_keys = delete_tenant_chat_data(tenant_id)
    return len(user_ids), deleted_chat_keys
