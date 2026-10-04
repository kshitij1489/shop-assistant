# app/utils/chat_session.py
import logging
from typing import Tuple, Dict, Any, Optional, Union

from django.db import transaction, IntegrityError
from django.utils import timezone
from functools import lru_cache
from orders.models import ChatSession, Order
from chatbot_core.scope import required_identity, normalize_platform

logger = logging.getLogger(__name__)

def get_chat_ongoing_session(session_id: str, *, tenant_id, platform):
    return ChatSession.objects.filter(
        tenant_id=required_identity(tenant_id, "tenant_id"),
        platform=normalize_platform(platform),
        session_id=required_identity(session_id, "session_id"), is_completed=False).order_by('-last_interaction_at', '-created_at', '-pk').first()

def create_new_chat_session(
    tenant,
    customer,
    platform: str,
    session_id: str,
    defaults: Optional[Dict[str, Any]] = None
) -> "ChatSession":
    """
    Create a brand-new ChatSession row while preserving the provided session_id
    on the newly-created row.

    Behavior:
      - If archive_existing is True and an existing ChatSession with the same
        (tenant, platform, session_id) exists, that existing row will be renamed
        to "<old_session_id>-archived-<uuid>" to free the original session_id.
        The existing row will remain in the DB (history preserved).
      - If mark_archived_completed is True and the model has `is_completed`, the
        archived row's is_completed will be set True.
      - The new ChatSession is then created with the original session_id and the
        provided defaults (defaults may include state, device_info, language, etc).
      - The operation runs inside a transaction and retries once on IntegrityError.

    Args:
        tenant: Tenant instance
        customer: Customer instance
        platform: platform string (e.g., "telegram")
        session_id: session identifier to keep on the new row (telegram phone number)
        defaults: dict of fields to set on create (e.g. language, state, device_info)
        archive_existing: whether to archive an existing session (rename it) if present
        mark_archived_completed: if True, set is_completed=True on archived row (if field exists)

    Returns:
        The newly-created ChatSession instance.

    Raises:
        IntegrityError or RuntimeError if creation ultimately fails.
    """
    defaults = dict(defaults or {})
    if customer is not None and customer.tenant_id != tenant.pk:
        raise ValueError('Customer belongs to another tenant.')
    # Defaults are state metadata, never an alternate ownership path.
    forbidden = {'tenant', 'tenant_id', 'customer', 'customer_id', 'order', 'order_id', 'platform', 'session_id'}
    defaults = {key: value for key, value in defaults.items() if key not in forbidden}
    # Ensure customer is present on create
    defaults.setdefault("customer", customer)

    # Try twice to handle rare concurrent races
    for attempt in range(2):
        try:
            with transaction.atomic():
                # Prepare kwargs for creation. Protect tenant/platform/session_id from being swapped by defaults.
                create_kwargs = {
                    "tenant": tenant,
                    "platform": platform,
                    "session_id": str(session_id),
                }
                for k, v in defaults.items():
                    if k in ("tenant", "platform", "session_id"):
                        continue
                    create_kwargs[k] = v

                # Ensure last_interaction_at exists on create if not provided
                if "last_interaction_at" not in create_kwargs:
                    create_kwargs["last_interaction_at"] = timezone.now()

                # Create the new ChatSession
                new_obj = ChatSession.objects.create(**create_kwargs)
                return new_obj

        except IntegrityError as exc:
            # Rare: race where another transaction recreated or didn't release unique key.
            logger.warning("IntegrityError on create_new_chat_session attempt %s: %s", attempt, exc)
            if attempt == 1:
                # Re-raise the IntegrityError so caller is aware
                raise

    # Shouldn't reach here
    raise RuntimeError("Failed to create new ChatSession after retries")

def update_chat_session_order(
    tenant,
    session_id: Union[str, int],
    order: "Order",
    platform: str
) -> "ChatSession":
    """
    Update the 'order' of the most recent active ChatSession (is_completed == False)
    identified by (tenant, platform, session_id). Also updates last_interaction_at.

    Raises ChatSession.DoesNotExist if no active session exists.
    """
    if order.tenant_id != tenant.pk:
        raise ValueError('Order belongs to another tenant.')
    with transaction.atomic():
        # Build queryset for active sessions with same session_id, ordered by recency.
        qs = (
            ChatSession.objects
            .filter(
                tenant=tenant,
                session_id=str(session_id),
                platform=normalize_platform(platform),
                is_completed=False
            )
            .order_by('-last_interaction_at', '-created_at', '-pk')  # tie-breakers
            .select_for_update()
        )

        # Pick the most recent active session (or None)
        session = qs.first()
        if session is None:
            # Mirror previous behavior: raise DoesNotExist so caller can handle it
            raise ChatSession.DoesNotExist(
                f"No active ChatSession found for tenant={tenant!r}, platform={platform!r}, session_id={session_id!r}"
            )

        # Update order and timestamp
        if session.customer_id != order.customer_id:
            raise ValueError('Order belongs to another customer.')
        session.order = order
        session.last_interaction_at = timezone.now()
        session.save(update_fields=["order", "last_interaction_at"])

        return session

def complete_chat_session_by_order(
    order: Order,
    mark_all: bool = False,
) -> Tuple[Optional[ChatSession], int]:
    """
    Mark ChatSession(s) associated with an Order as completed.

    Args:
        order: Order primary key (int) or str convertible to int.
        mark_all: If True, mark all ChatSession rows with this order as completed.
                  If False (default), mark only the most-recent ChatSession by last_interaction_at.

    Returns:
        (updated_session, count)
          - If mark_all is False: returns (ChatSession instance updated, 1) on success,
            or (None, 0) if no session found.
          - If mark_all is True: returns (None, n) where n is number of sessions updated.

    Raises:
        Order.DoesNotExist if the order is invalid.
    """
    now = timezone.now()

    if mark_all:
        # Update all sessions referencing this order in a single atomic query.
        # Using select_for_update is not needed for a bulk update, but we still use a transaction.
        with transaction.atomic():
            updated_count = ChatSession.objects.filter(order=order, tenant_id=order.tenant_id).update(
                is_completed=True,
                last_interaction_at=now
            )
        return None, updated_count

    # Default: update the single most-recent session (if any)
    with transaction.atomic():
        # Find the most recent session for this order
        session_qs = ChatSession.objects.select_for_update().filter(order=order, tenant_id=order.tenant_id).order_by('-last_interaction_at')

        try:
            session = session_qs[0]  # Indexing a queryset executes it
        except IndexError:
            # No session found for this order
            return None, 0

        session.is_completed = True
        session.last_interaction_at = now
        session.save(update_fields=["is_completed", "last_interaction_at"])

        return session
