"""Compact, tenant-scoped ordering catalog and authoritative selection validation."""
from decimal import Decimal, InvalidOperation
from django.db.models import Q
from commerce.policy import MAX_ITEM_QUANTITY, MAX_QUANTITY_DIGITS
from orders.models import MenuItem
from .ordering_errors import OrderingRejected


def serialize_item(item):
    return {
        "item_id": str(item.pk), "name": item.name,
        "aliases": list((item.meta or {}).get("aliases", [])),
        "variants": [{"id": str(v.pk), "name": v.size, "aliases": v.aliases,
                      "volume_ml": v.volume_ml, "weight_grams": v.weight_grams,
                      "price": str(v.price)} for v in item.variants.all() if v.is_available],
        "modifier_groups": [
            {"id": str(link.group_id), "name": link.group.name,
             "min": link.min_selections, "max": link.max_selections,
             "variant_ids": link.variant_ids,
             "options": [{"id": str(a.pk), "name": a.name, "aliases": a.aliases,
                          "price": str(a.price), "min_quantity": a.min_quantity,
                          "max_quantity": a.max_quantity}
                         for a in link.group.addons.all() if a.is_available]}
            for link in item.addon_groups.all() if link.tenant_id == item.tenant_id
            and link.group.tenant_id == item.tenant_id],
    }


def available_items(api_key):
    return (MenuItem.objects.filter(tenant__api_key=api_key, is_available=True)
            .filter(Q(category_fk__isnull=True) | Q(category_fk__is_active=True)))


def load_catalog(api_key):
    items = available_items(api_key).prefetch_related("variants", "addon_groups__group__addons")
    return {str(item.pk): serialize_item(item) for item in items}


def catalog_names(api_key):
    """Id, name and alias rows for product ambiguity checks: one query, no variants or modifiers."""
    return [{"id": str(pk), "name": name, "aliases": list((meta or {}).get("aliases", []))}
            for pk, name, meta in available_items(api_key).values_list("pk", "name", "meta")]


def positive_integer(value):
    """Positive whole number, or None. Oversized digit strings are not converted."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, int):
        # bit_length rejects enormous ints before their decimal form is built.
        if value < 1 or value.bit_length() > 40 or len(str(value)) > MAX_QUANTITY_DIGITS:
            return None
        return value
    text = value.strip()
    if len(text) > MAX_QUANTITY_DIGITS or not text.isascii() or not text.isdigit():
        return None
    number = int(text)
    return number if number > 0 else None


def money(value):
    try:
        amount = Decimal(str(value))
        if amount.is_finite() and amount >= 0:
            return amount
    except (InvalidOperation, TypeError, ValueError):
        pass
    raise ValueError("Pricing is unavailable. Please choose another item.")


def validate_selection(catalog, item_id, variant_id, quantity, modifiers):
    if type(quantity) is int and quantity > MAX_ITEM_QUANTITY:
        raise OrderingRejected('That quantity is above the supported maximum. Please request a smaller quantity.')
    item = catalog.get(str(item_id))
    if not item:
        raise ValueError("Which menu item would you like? Please choose an available item.")
    variant = next((v for v in item["variants"] if v["id"] == variant_id), None)
    if not variant:
        raise ValueError(f"Which size of {item['name']} would you like? Available sizes: "
                         + ", ".join(v["name"] for v in item["variants"]) + ".")
    qty = positive_integer(quantity)
    if qty is None:
        raise ValueError("How many would you like? Please give a positive whole number.")
    if qty > MAX_ITEM_QUANTITY:
        raise OrderingRejected('That quantity is above the supported maximum. Please request a smaller quantity.')
    if not isinstance(modifiers, list):
        raise ValueError("Please specify the modifier choices.")
    from .ordering_limits import MAX_MODIFIER_CHOICES
    if len(modifiers) > MAX_MODIFIER_CHOICES:
        raise ValueError("Too many modifier choices were supplied.")
    groups = {g["id"]: g for g in item["modifier_groups"]
              if not g["variant_ids"] or variant_id in g["variant_ids"]}
    counts, seen, normalized = {}, set(), []
    base_price = money(variant["price"])
    for choice in modifiers:
        group = groups.get(str(choice.get("group_id"))) if isinstance(choice, dict) else None
        option = next((o for o in group["options"] if o["id"] == str(choice.get("option_id"))), None) if group else None
        count = positive_integer(choice.get("quantity")) if isinstance(choice, dict) else None
        if not option or not count or not option["min_quantity"] <= count <= option["max_quantity"]:
            raise ValueError("That customization or quantity is unavailable for this item and size. Please choose a permitted option.")
        key = (group["id"], option["id"])
        if key in seen:
            raise ValueError("Please give each modifier choice once, with its quantity.")
        seen.add(key)
        counts[group["id"]] = counts.get(group["id"], 0) + 1
        surcharge = money(option["price"])
        normalized.append({"group_id": group["id"], "option_id": option["id"],
                           "name": option["name"], "quantity": count, "unit_price": str(surcharge)})
    for group in groups.values():
        if not group["min"] <= counts.get(group["id"], 0) <= group["max"]:
            raise ValueError(f"Choose {group['min']}–{group['max']} options for {group['name']}: "
                             + ", ".join(o["name"] for o in group["options"]) + ".")
    return {"name": item["name"], "size": variant["name"], "item_id": item["item_id"],
            "item_variant_id": variant["id"], "quantity": qty, "unit_price": str(base_price),
            "modifiers": sorted(normalized, key=lambda m: (m["group_id"], m["option_id"]))}


def modifier_key(modifiers):
    return tuple(sorted((str(m["group_id"]), str(m["option_id"]), m["quantity"]) for m in (modifiers or [])))


def selection_unit_total(selection):
    """Base variant price plus modifiers for one purchased item."""
    return money(selection['unit_price']) + sum(
        (money(m['unit_price']) * m['quantity'] for m in selection.get('modifiers', [])),
        Decimal(0),
    )


def selection_prices_match(entry, selection):
    """Compare each component so offsetting catalog changes still need review."""
    def prices(row):
        return sorted((str(m['group_id']), str(m['option_id']), m['quantity'], money(m.get('unit_price')))
                      for m in row.get('modifiers', []))
    return money(entry['unit_price']) == money(selection['unit_price']) and prices(entry) == prices(selection)


def selection_price_changes(entry, selection):
    """Read-only component comparison against an authoritative validated selection.

    Keep base and modifier changes separate: an unchanged total can conceal
    offsetting price changes. Amounts are major-unit decimal strings, not prose.
    """
    if (str(entry['item_id']) != str(selection['item_id'])
            or str(entry['item_variant_id']) != str(selection['item_variant_id'])
            or modifier_key(entry.get('modifiers')) != modifier_key(selection.get('modifiers'))):
        raise ValueError('The selection changed. Please review the item and customizations.')
    changes = []

    def compare(kind, name, quantity, old, current):
        old, current = money(old), money(current)
        if old != current:
            changes.append({'kind': kind, 'name': name, 'quantity': quantity,
                            'previous_unit_price': str(old), 'current_unit_price': str(current)})

    compare('variant', selection['size'], 1, entry['unit_price'], selection['unit_price'])
    old_modifiers = {(str(m['group_id']), str(m['option_id'])): m for m in entry.get('modifiers', [])}
    for modifier in selection.get('modifiers', []):
        old = old_modifiers[str(modifier['group_id']), str(modifier['option_id'])]
        compare('modifier', modifier['name'], modifier['quantity'],
                old['unit_price'], modifier['unit_price'])
    return changes
