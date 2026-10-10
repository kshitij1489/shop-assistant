from __future__ import annotations
from chatbot_core.models import TenantInfo
from orders.models import Order, OrderItem, OrderItemAddon, Customer, MenuItem, MenuItemVariant, CustomerAddress
from django.db import transaction
from django.utils.timezone import localtime
from django.db.models import Q
from decimal import Decimal
from typing import List, Optional, Tuple, Dict
import uuid
from .basket import Basket
from .catalog import selection_prices_match, selection_unit_total
import datetime
from django.db.models.functions import Coalesce

def create_or_get_customer(tenant, platform: str, phone: Optional[str] = None,
    name: Optional[str] = None, whatsapp_number: Optional[str] = None,
    telegram_id: Optional[str] = None, address: Optional[str] = "",
    location_coordinates: Optional[dict] = None, external_id: Optional[str] = None) -> Customer:
    """
    Creates or returns existing customer for a given platform.
    - WhatsApp: use phone or whatsapp_number
    - Telegram: use telegram_id
    - Website: fallback to phone or anonymous
    """

    if platform == 'voiceassistant' and external_id:
        from orders.models import ChatSession
        previous = ChatSession.objects.filter(
            tenant=tenant, platform=platform, session_id=str(external_id), customer__tenant=tenant,
        ).select_related('customer').order_by('-created_at').first()
        if previous:
            return previous.customer

    filters = {"tenant": tenant}

    if platform == "whatsapp" and (phone or whatsapp_number):
        if phone:
            filters["phone"] = phone
        elif whatsapp_number:
            filters["whatsapp_number"] = whatsapp_number

    elif platform == "telegram" and telegram_id:
        filters["telegram_id"] = telegram_id

    elif platform == "website" and phone:
        filters["phone"] = phone
    else:
        # Generate anonymous fallback (you can later extend this)
        name = name or "Anonymous User"
        phone = ""

    customer = Customer.objects.filter(**filters).first() if len(filters) > 1 else None

    if customer:
        return customer

    # Create new customer if not found
    customer = Customer.objects.create(
        tenant=tenant,
        name=name or "Guest",
        phone=phone or "",
        whatsapp_number=whatsapp_number,
        telegram_id=telegram_id,
        address=address or "",
        location_coordinates=location_coordinates or {}
    )
    return customer

@transaction.atomic
def create_order(tenant, customer, items_or_basket, chat_id, source="inhouse",payment_mode=None, catalog_tax=True):
    """
    Create an order from either a list of items or a Basket object.

    Args:
        tenant: TenantInfo instance
        customer: Customer instance
        items_or_basket: List[dict] or Basket
        source: order source (e.g., 'inhouse', 'whatsapp', etc.)
        payment_mode: optional payment method

    Returns:
        Order instance
    """

    if not customer or str(customer.tenant_id) != str(tenant.pk):
        raise ValueError("Customer does not belong to the order tenant.")
    from commerce.menu_sync import lock_menu, assert_menu_fresh, source_for
    lock_menu(tenant.pk)
    assert_menu_fresh(tenant.pk)
    menu_source = source_for(tenant.pk)
    if menu_source and menu_source.mode == 'external' and not isinstance(items_or_basket, Basket):
        raise ValueError('Externally managed menus require a catalog-validated basket.')

    # Normalize items from either basket or item list
    normalized_items = []

    if isinstance(items_or_basket, Basket):
        from .catalog import load_catalog, validate_selection
        catalog = load_catalog(tenant.api_key)
        for entry in items_or_basket.items:
            name = entry.get("name")
            size = entry.get("size")
            quantity = entry.get("quantity")

            if not all([name, size, quantity]):
                raise ValueError("Incomplete basket entry")

            try:
                item_lookup = {"pk": entry["item_id"]} if entry.get("item_id") else {"name__iexact": name}
                variant_lookup = {"pk": entry["item_variant_id"]} if entry.get("item_variant_id") else {"size__iexact": size}
                menu_item = MenuItem.objects.get(tenant=tenant, **item_lookup)
                variant = MenuItemVariant.objects.get(menu_item=menu_item, is_available=True, **variant_lookup)
            except MenuItem.DoesNotExist:
                raise ValueError(f"Menu item not found: {name}")
            except MenuItemVariant.DoesNotExist:
                raise ValueError(f"Variant not found for {name} with size {size}")

            if entry.get("item_id") and entry["item_id"] != str(menu_item.pk):
                raise ValueError("Basket item identity changed")
            if entry.get("item_variant_id") and entry["item_variant_id"] != str(variant.pk):
                raise ValueError("Basket variant identity changed")
            selection = validate_selection(catalog, str(menu_item.pk), str(variant.pk), quantity, entry.get("modifiers", []))
            if not selection_prices_match(entry, selection):
                raise ValueError("Catalog price changed; review the basket before checkout")
            normalized_items.append({"item": menu_item, "variant": variant,
                                     "item_name": f"{name} ({size})", "quantity": selection["quantity"],
                                     "unit_price": selection["unit_price"], "modifiers": selection["modifiers"]})

    elif isinstance(items_or_basket, list):
        normalized_items = items_or_basket
    else:
        raise ValueError("items_or_basket must be either a Basket or a list of item dicts.")

    # The legacy list path must enforce ownership just like catalog proposals.
    modifier_names = {}
    for entry in normalized_items:
        item, variant = entry.get('item'), entry.get('variant')
        if item is not None and str(item.tenant_id) != str(tenant.pk):
            raise ValueError('Item belongs to another tenant.')
        if variant is not None and (str(variant.menu_item.tenant_id) != str(tenant.pk) or
                                    (item is not None and variant.menu_item_id != item.pk)):
            raise ValueError('Variant does not belong to this tenant/item.')
        from orders.models import AddonItem
        for modifier in entry.get('modifiers', []):
            option = AddonItem.objects.filter(pk=modifier['option_id'], group__tenant=tenant).first()
            if option is None:
                raise ValueError('Modifier belongs to another tenant.')
            modifier_names[str(option.pk)] = option.name

    from .ordering_limits import load_policy
    ordering_policy = load_policy(tenant_id=tenant.pk)
    order_meta = {'currency': ordering_policy.currency if ordering_policy else 'INR',
                  'exponent': ordering_policy.exponent if ordering_policy else 2}
    if chat_id:
        order_meta["chat_id"] = chat_id
    # Compute total and create Order
    total = sum((selection_unit_total(item) * item["quantity"] for item in normalized_items), Decimal(0))
    from orders.pricing import catalog_taxes
    tax, snapshots = catalog_taxes(tenant, normalized_items) if catalog_tax else (Decimal(0), [[] for _ in normalized_items])
    order = Order.objects.create(tenant=tenant, source=source, customer=customer, total_amount=total + tax, tax_amount=tax, payment_mode=payment_mode, meta=order_meta)

    # Add OrderItems
    for item, taxes in zip(normalized_items, snapshots):
        saved_item = OrderItem.objects.create(
            order=order, item=item.get("item"), variant=item.get("variant"), item_name=item["item_name"],
            quantity=item["quantity"], unit_price=item["unit_price"], item_tax_snapshot=taxes,
            total_price=Decimal(item["unit_price"]) * item["quantity"]
        )
        for modifier in item.get("modifiers", []):
            OrderItemAddon.objects.create(
                order_item=saved_item, addon_id=modifier["option_id"], quantity=modifier["quantity"],
                addon_name=modifier_names[str(modifier['option_id'])],
                unit_price=modifier["unit_price"],
                total_price=Decimal(modifier["unit_price"]) * modifier["quantity"] * item["quantity"],
            )

    return order

def get_order_history(customer: Customer) -> str:
    """Read the latest five orders, including orders whose items were removed."""
    if customer is None or not getattr(customer, 'pk', None):
        return 'I don’t have a verified customer record here to check your orders.'
    recent_orders = list(_enquiry_orders(customer).prefetch_related("items")[:5])
    if not recent_orders:
        return "I couldn’t find any orders in the records available to this chat."
    lines = ["Here are your five most recent orders (or all orders if fewer):"]
    for order in recent_orders:
        order_time = localtime(order.created_at).strftime("%b %d, %Y at %I:%M %p")
        items = ", ".join(f"{item.item_name or 'Item'} x{item.quantity}" for item in order.items.all())
        lines.append(
            f"Order {order.id} — {order_time} — {items or 'Item details unavailable'} "
            f"[Status: {_status_label(order.order_status)}; "
            f"Payment: {_status_label(order.payment_status)}; Total: {order.total_amount}]"
        )
    return "\n".join(lines)

# 3. Show status of the most recent order
def get_current_order_status(customer: Customer) -> Optional[str]:
    latest = _enquiry_orders(customer).first()
    return latest.order_status if latest else None

# 4. Status on payment of latest order
def get_payment_status(customer: Customer) -> Optional[str]:
    latest = _enquiry_orders(customer).first()
    return latest.payment_status if latest else None

# 5. Delivery status of latest order
def get_delivery_status(customer: Customer) -> Optional[str]:
    latest = _enquiry_orders(customer).first()
    if latest and latest.delivery_partner:
        return latest.delivery_partner.status
    return None

# 6. Get order details for a specific order id
def get_order_details(order_id: uuid.UUID, tenant: TenantInfo) -> Optional[dict]:
    try:
        order = Order.objects.get(id=order_id, tenant=tenant)
        items = order.items.all()
        details = {
            "id": str(order.id),
            "status": order.order_status,
            "payment_status": order.payment_status,
            "source": order.source,
            "created_at": order.created_at,
        }
        from commerce.pricing import minor
        from .ordering_limits import load_policy
        policy = load_policy(tenant_id=tenant.pk)
        if policy is None:
            return details
        return {
            **details,
            "currency": policy.currency,
            "exponent": policy.exponent,
            "total_amount_minor": minor(order.total_amount, policy.exponent),
            "items": [
                {
                    "name": item.item_name,
                    "quantity": item.quantity,
                    "unit_price_minor": minor(item.unit_price, policy.exponent),
                    "total_price_minor": minor(item.total_price, policy.exponent),
                } for item in items
            ],
        }
    except Order.DoesNotExist:
        return None

# --- Helpers for support text ---
def support_contacts_line(tenant: TenantInfo) -> str:
    # TenantInfo stores optional support configuration in meta.
    meta = tenant.meta if isinstance(getattr(tenant, "meta", None), dict) else {}
    email = getattr(tenant, "support_email", None) or meta.get("support_email")
    phone = getattr(tenant, "support_phone", None) or meta.get("support_phone")
    parts = []
    if isinstance(email, str) and email.strip():
        parts.append(f"email: {email.strip()}")
    if isinstance(phone, str) and phone.strip():
        parts.append(f"phone/WhatsApp: {phone.strip()}")
    return ("You can reach support at " + ", ".join(parts) + ".") if parts else (
        "Please contact the café directly for assistance."
    )


def _enquiry_orders(customer: Customer, tenant: Optional[TenantInfo] = None):
    """Never query anonymous orders or orders outside the customer's tenant."""
    if customer is None or not getattr(customer, "pk", None) or not getattr(customer, "tenant_id", None):
        return Order.objects.none()
    tenant_id = tenant.pk if tenant is not None else customer.tenant_id
    if str(tenant_id) != str(customer.tenant_id):
        return Order.objects.none()
    return Order.objects.filter(customer=customer, tenant_id=tenant_id).order_by("-created_at", "-id")


def _enquiry_order(customer: Customer, order_id=None, tenant: Optional[TenantInfo] = None) -> Optional[Order]:
    orders = _enquiry_orders(customer, tenant).select_related("delivery_partner")
    if order_id is not None:
        # External receipt IDs are strings; only valid UUIDs reach the UUIDField.
        # An explicit missing/foreign reference must never fall back to latest.
        reference = str(order_id).strip()
        if not reference:
            return None
        lookup = Q(external_order_id=reference)
        try:
            lookup |= Q(pk=uuid.UUID(reference))
        except (ValueError, AttributeError):
            pass
        matches = list(orders.filter(lookup)[:2])
        return matches[0] if len(matches) == 1 else None
    return orders.first()


def _status_label(value) -> str:
    return (value or "unknown").replace("_", " ").capitalize()


def _order_not_found(tenant, order_id=None) -> str:
    message = ("I couldn’t find that order in the records available to this chat. Check the order ID on your receipt. "
               if order_id is not None else "I couldn’t find any orders in the records available to this chat. ")
    return message + support_contacts_line(tenant)


def _delivery_lines(order) -> list[str]:
    partner = order.delivery_partner
    if not partner or partner.tenant_id != order.tenant_id:
        return []
    lines = [f"• Delivery partner: {partner.name} ({_status_label(partner.status)})"]
    if partner.tracking_url:
        lines.append(f"• Tracking link: {partner.tracking_url}")
    return lines


def order_status(customer: Customer, order_id=None) -> Optional[str]:
    order = _enquiry_order(customer, order_id)
    if not order:
        return None
    lines = [f"{'Latest order' if order_id is None else 'Order'}: {order.id}",
             f"• Placed on: {localtime(order.created_at).strftime('%b %d, %Y at %I:%M %p')}",
             f"• Status: {_status_label(order.order_status)}",
             f"• Payment: {_status_label(order.payment_status)}"]
    return "\n".join(lines + _delivery_lines(order))


def missing_or_wrong_items(tenant: TenantInfo, customer: Customer, order_id=None) -> str:
    from .order_support import store_call_response
    return store_call_response(tenant)


def refund_and_cancellation(tenant: TenantInfo, customer: Customer, order_id=None) -> str:
    from .order_support import store_call_response
    return store_call_response(tenant)


def address_change_enquiry(tenant: TenantInfo, customer: Customer, order_id=None) -> str:
    """Read-only support guidance, separate from the profile mutation helper."""
    from .order_support import store_call_response
    return store_call_response(tenant)

# 4) Address or contact update
def address_or_contact_update(
    tenant: TenantInfo,
    customer: Customer,
    *,
    new_address: Optional[str] = None,
    new_phone: Optional[str] = None,
    apply_globally: bool = True,
    order_id: Optional[uuid.UUID] = None,
) -> str:
    """Update profile details only; placed-order requests always go to the store."""
    if order_id is not None:
        from .order_support import store_call_response
        return store_call_response(tenant)
    if customer.tenant_id != tenant.pk:
        raise ValueError("Customer belongs to another tenant.")

    # Optionally update the Customer profile
    updated_fields = []
    if apply_globally:
        if new_address and new_address.strip():
            customer.address = new_address.strip()
            updated_fields.append("address")
        if new_phone and new_phone.strip():
            customer.phone = new_phone.strip()
            updated_fields.append("phone")
        if updated_fields:
            customer.save(update_fields=updated_fields)

    if not new_address and not new_phone:
        return "Share the new address/phone and I’ll update it. " + support_contacts_line(tenant)

    confirm = "Updated your details" if updated_fields else "Noted your request"
    scope = " for your profile" if updated_fields else ""
    return f"{confirm}{scope}. " + support_contacts_line(tenant)

# 5) Delivery problems
def delivery_problems(tenant: TenantInfo, customer: Customer, order_id=None) -> str:
    from .order_support import store_call_response
    return store_call_response(tenant)


def general_order_enquiry(tenant: TenantInfo, customer: Customer, question: Optional[str] = None) -> str:
    from .order_support import store_call_response
    return ("I can show your order status or your five most recent orders. "
            + store_call_response(tenant))

# ---------- Address queries ----------

def list_addresses(customer: Customer) -> List[CustomerAddress]:
    return list(
        CustomerAddress.objects.filter(tenant_id=customer.tenant_id, customer=customer).order_by("-is_default", "-updated_at")
    )

def get_default_address(customer: Customer) -> Optional[CustomerAddress]:
    return CustomerAddress.objects.filter(tenant_id=customer.tenant_id, customer=customer, is_default=True).first()

def get_latest_order(customer: Customer) -> Optional[Order]:
    return _enquiry_orders(customer).first()

def get_order_by_id(tenant: TenantInfo, customer: Customer, order_id: uuid.UUID) -> Optional[Order]:
    return Order.objects.filter(id=order_id, tenant=tenant, customer=customer).first()

# ---------- Customer <-> Default Address mirroring ----------

def _extract_pincode_from_text(text: str | None) -> str | None:
    if not text:
        return None
    import re
    m = re.findall(r"\b(\d{6})\b", text)
    return m[-1] if m else None

@transaction.atomic
def mirror_default_to_customer(customer: Customer) -> None:
    """
    Mirror the current default CustomerAddress into customer.location_coordinates.
    If no default exists but at least one address exists, promote most recent to default and mirror.
    If no addresses remain, clear location_coordinates.
    """
    default = get_default_address(customer)
    if not default:
        # Try to promote one
        next_addr = CustomerAddress.objects.filter(tenant_id=customer.tenant_id, customer=customer).order_by("-updated_at").first()
        if next_addr:
            next_addr.is_default = True
            next_addr.save(update_fields=["is_default", "updated_at"])
            default = next_addr

    if default:
        customer.location_coordinates = {
            "address": default.address_line,
            "label": default.label,
            "pincode": _extract_pincode_from_text(default.address_line),
        }
    else:
        customer.location_coordinates = None

    # If your Customer has an `updated_at` field, include it here
    customer.save(update_fields=["location_coordinates"])

# ---------- Mutations (DB-only) ----------

@transaction.atomic
def ensure_single_default(customer: Customer, keep_id: uuid.UUID) -> None:
    CustomerAddress.objects.filter(tenant_id=customer.tenant_id, customer=customer).exclude(id=keep_id).update(is_default=False)

@transaction.atomic
def create_address(
    *,
    tenant: TenantInfo,
    customer: Customer,
    formatted_address: str,
    components: Dict[str, Optional[str]],
    label: Optional[str] = None,
    set_as_default: bool = True,
) -> CustomerAddress:
    if customer.tenant_id != tenant.pk:
        raise ValueError('Customer belongs to another tenant.')
    addr = CustomerAddress.objects.create(
        tenant=tenant,
        customer=customer,
        label=(label or None),
        address_line=formatted_address,
        location_coordinates=None,
        components=components,
        is_default=False,
    )
    if set_as_default:
        ensure_single_default(customer, addr.id)
        addr.is_default = True
        addr.save(update_fields=["is_default", "updated_at"])

    # 🔑 Always mirror if this is default OR if there is no other default
    if set_as_default:
        mirror_default_to_customer(customer)
    elif not get_default_address(customer):
        # No default existed before; make this one default & mirror
        addr.is_default = True
        addr.save(update_fields=["is_default", "updated_at"])
        ensure_single_default(customer, addr.id)
        mirror_default_to_customer(customer)

    return addr

@transaction.atomic
def update_address_fields(
    *,
    address_id: uuid.UUID,
    customer: Customer,
    label: Optional[str] = None,
    formatted_address: Optional[str] = None,
    components: Optional[Dict[str, Optional[str]]] = None,
    set_as_default: Optional[bool] = None,
) -> Optional[CustomerAddress]:
    addr = CustomerAddress.objects.filter(id=address_id, customer=customer, tenant_id=customer.tenant_id).first()
    if not addr:
        return None

    changed_fields: List[str] = []

    if label is not None:
        addr.label = label or None
        changed_fields.append("label")

    if formatted_address is not None:
        addr.address_line = formatted_address
        # A text correction must never retain coordinates from an older address.
        addr.location_coordinates = None
        changed_fields.extend(["address_line", "location_coordinates"])

    if components is not None:
        addr.components = components
        changed_fields.append("components")


    if set_as_default is True:
        ensure_single_default(customer, addr.id)
        if not addr.is_default:
            addr.is_default = True
            changed_fields.append("is_default")
    elif set_as_default is False:
        if addr.is_default:
            addr.is_default = False
            changed_fields.append("is_default")

    if changed_fields:
        addr.save(update_fields=changed_fields + ["updated_at"])

    # 🔑 If this address is (now) default OR we changed its address_line/coords while it was default, mirror to Customer
    is_default = CustomerAddress.objects.filter(id=addr.id, customer=customer, is_default=True).exists()
    if is_default:
        mirror_default_to_customer(customer)

    return addr

@transaction.atomic
def delete_address(customer: Customer, address_id: uuid.UUID) -> Tuple[bool, bool]:
    """
    Returns (found, was_default)
    """
    addr = CustomerAddress.objects.filter(id=address_id, customer=customer, tenant_id=customer.tenant_id).first()
    if not addr:
        return (False, False)
    was_default = addr.is_default
    addr.delete()

    if was_default:
        # Promote another address if available and mirror to customer (or clear if none)
        mirror_default_to_customer(customer)
    else:
        # If there is still a default, keep it in sync (no-op); if none remains, mirror clears it
        if not get_default_address(customer):
            mirror_default_to_customer(customer)

    return (True, was_default)

@transaction.atomic
def pick_new_default_if_needed(customer: Customer) -> Optional[CustomerAddress]:
    """
    If no default exists, promote most recent and mirror; if none exists, clear customer coordinates.
    """
    default = get_default_address(customer)
    if default:
        return default
    next_addr = CustomerAddress.objects.filter(tenant_id=customer.tenant_id, customer=customer).order_by("-updated_at").first()
    if next_addr:
        next_addr.is_default = True
        next_addr.save(update_fields=["is_default", "updated_at"])
        mirror_default_to_customer(customer)
        return next_addr
    # No addresses left — ensure customer is cleared
    mirror_default_to_customer(customer)
    return None

# ---------- Delivery coverage ----------

def verify_delivery_pincode(tenant: TenantInfo, pincode_or_locality: str) -> Optional[bool]:
    from evaluate.controls.context import fault_active
    if fault_active('coverage', tenant.pk):
        return None
    if isinstance(pincode_or_locality, bool) or not isinstance(pincode_or_locality, (str, int)):
        return None
    val = str(pincode_or_locality).strip()
    if not val:
        return None

    from chatbot_core.logic.cafe.location_utils import normalize_pincode

    is_pincode = normalize_pincode(val) is not None
    from commerce.models import Configuration
    meta = getattr(tenant, 'meta', None)
    setup_required = isinstance(meta, dict) and meta.get('ordering_setup_required')
    if not setup_required and Configuration.objects.filter(tenant=tenant, local_checkout=True).exists():
        from orders.models import CheckoutSettings
        from orders.checkout_config import CheckoutPolicy
        row = CheckoutSettings.objects.filter(tenant=tenant).first()
        if not row:
            return None
        try:
            checkout = CheckoutPolicy.model_validate(row.configuration)
        except ValueError:
            return None
        if 'delivery' not in checkout.modes:
            return False
        if not is_pincode:
            return None
        return not checkout.delivery_postal_codes or val.upper() in checkout.delivery_postal_codes
    meta = getattr(tenant, "meta", None)
    if not isinstance(meta, dict):
        return None
    pins = meta.get("serviceable_pincodes")
    if is_pincode and isinstance(pins, (list, set, tuple)):
        pins = {normalize_pincode(pin) for pin in pins}
        if None in pins:
            return None
        return val in pins

    locs = meta.get("serviceable_localities")
    if not is_pincode and isinstance(locs, (list, set, tuple)) and all(isinstance(x, str) for x in locs):
        locs = {x.lower().strip() for x in locs}
        return val.lower() in locs

    return None

def get_most_recent_order(
    *,
    tenant: TenantInfo,
    customer: Optional[Customer] = None,
    include_cancelled: bool = False,
    include_failed_payments: bool = False,
) -> Optional[Order]:
    """
    Return the single most recent Order for this tenant (optionally scoped to a customer).
    “Most recent” prefers created_at, falls back to id as a tie-breaker.
    """

    qs = Order.objects.filter(tenant=tenant)

    if customer is not None:
        qs = qs.filter(customer=customer)

    if not include_cancelled:
        qs = qs.exclude(order_status=Order.Status.CANCELLED)

    if not include_failed_payments:
        qs = qs.exclude(payment_status=Order.PaymentStatus.FAILED)

    # Be robust to any null created_at (shouldn't happen with auto_now_add, but safe)
    qs = qs.annotate(
        _sort_created=Coalesce("created_at", datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc))
    ).order_by("-_sort_created", "-id")

    # If you have related items/variants, feel free to prefetch here:
    # qs = qs.select_related("customer", "delivery_partner").prefetch_related("items", "items__menu_item", "items__variant")

    return qs.first()
