"""Transactional catalog import shared by dashboard and provisioning callers."""
import json
from decimal import Decimal, InvalidOperation
from django.db import transaction
from orders.models import MenuItem, MenuItemVariant, MenuCategory, MenuCatalogMeta
from chatbot_core.configuration_files import parse_json, catalog_knowledge


def parse_price(value):
    try:
        amount = Decimal(str(value).removeprefix('₹').strip())
    except InvalidOperation as exc:
        raise ValueError('Prices must be decimal amounts.') from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError('Prices must be finite and nonnegative.')
    return amount


def validate_catalog(data):
    if not isinstance(data, dict) or not isinstance(data.get('menu_items'), list):
        raise ValueError("Root must contain a menu_items list.")
    names = set()
    for row in data['menu_items']:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'].strip():
            raise ValueError('Each menu item needs a name.')
        if 'flavor_profile' in row and not isinstance(row['flavor_profile'], str):
            raise ValueError('Flavor profile must be text.')
        if 'menu_category' in row and not isinstance(row['menu_category'], str):
            raise ValueError('Menu category must be text.')
        if 'portion_and_size' in row and not isinstance(row['portion_and_size'], dict):
            raise ValueError('Portions must be an object.')
        name = row['name'].strip().casefold()
        if name in names:
            raise ValueError('Duplicate menu item name.')
        names.add(name)
        availability = row.get('availability', {})
        if not isinstance(availability, dict):
            raise ValueError('Availability must be an object.')
        quantity = availability.get('quantity', 0)
        if type(quantity) is not int or quantity < 0:
            raise ValueError('Quantity must be a nonnegative integer.')
        if 'is_available' in availability and type(availability['is_available']) is not bool:
            raise ValueError('Availability must be boolean.')
        pricing = row.get('pricing')
        if not isinstance(pricing, dict) or not pricing:
            raise ValueError('Each item needs variant prices.')
        labels = set()
        for label, price in pricing.items():
            if not isinstance(label, str) or not label.strip() or label.strip().casefold() in labels:
                raise ValueError('Variant labels must be nonempty and unique.')
            labels.add(label.strip().casefold())
            parse_price(price)
    if 'knowledge' in data:
        catalog_knowledge(data)
    return data


def _parse_bool(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "1", "yes", "y"}
    return False

def _normalize_bool_dict(d):
    if not isinstance(d, dict):
        return d
    return {k: _parse_bool(v) for k, v in d.items()}

def _to_json(val, default):
    """Accept plain Python types; if a string isn't JSON, wrap it as {'text': ...}."""
    if val is None:
        return default
    if isinstance(val, str):
        try:
            # Only valid JSON (true/false/null/lists/objects/numbers/strings) will parse.
            return json.loads(val)
        except Exception:
            return {"text": val}
    return val


def _catalog_knowledge(tenant, data, imported_ids):
    """Project the retained catalog after an upsert, keeping reviewed topic facts."""
    items = list(MenuItem.objects.filter(tenant=tenant).select_related('category_fk')
                 .prefetch_related('variants').order_by('name', 'pk'))
    by_id = {item.pk: item for item in items}
    imported = set(imported_ids)
    # Keep file order for complete imports and append existing offered items.
    ordered = [by_id[pk] for pk in imported_ids]
    ordered.extend(item for item in items if item.pk not in imported and item.is_available)
    rows = []
    for item in ordered:
        variants = [variant for variant in item.variants.all() if variant.is_available]
        if not variants:
            continue
        listing = (item.meta or {}).get('catalog_listing', {})
        selected = next((v for v in variants if v.size == listing.get('listed_variant')), None)
        # Older/manual items have no reviewed serving-size label.
        size = listing.get('listed_size') if selected else None
        selected = selected or variants[0]
        rows.append({
            'name': item.name,
            'menu_category': item.category_fk.name if item.category_fk else 'Uncategorized',
            'pricing': {v.size: format(v.price.normalize(), 'f') for v in variants},
            'listed_variant': selected.size,
            'listed_size': size,
        })
    return catalog_knowledge({**data, 'menu_items': rows})


@transaction.atomic
def import_catalog(tenant, source):
    from commerce.menu_sync import lock_menu, assert_local_menu
    from chatbot_core.configuration_imports import import_documents
    from users.utils import generate_menu_items_json
    lock_menu(tenant.pk)
    assert_local_menu(tenant.pk)
    data = validate_catalog(parse_json(source))
    if 'currency' in data:
        from commerce.models import Configuration
        config = Configuration.objects.filter(tenant=tenant).first()
        currency = config.policy['currency'] if config else 'INR'
        if data['currency'] != currency:
            raise ValueError('Catalog currency must match the ordering policy.')
    created = updated = v_added = v_updated = 0
    imported_ids = []
    for row in data["menu_items"]:
        name = (row.get("name") or "").strip()
        if not name:
            continue

        # category
        cat_norm = str(row.get("menu_category") or "").strip()
        cat_obj = None
        if cat_norm:
            cat_obj, _ = MenuCategory.objects.get_or_create(tenant=tenant, name__iexact=cat_norm, defaults={"name": cat_norm})
            cat_obj.full_clean()

        # item
        avail = row.get("availability") or {}
        qty_in = int((avail.get("quantity") or 0) or 0)

        # Source changes may leave disabled historical rows with the
        # same label. Prefer the currently offered item for local edits.
        item = MenuItem.objects.filter(tenant=tenant, name=name).order_by('-is_available', 'pk').first()
        was_created = item is None
        if was_created:
            item = MenuItem.objects.create(tenant=tenant, name=name, quantity=qty_in,
                is_available=qty_in > 0, description=(row.get('flavor_profile') or '')[:1000],
                category_fk=cat_obj, meta={})
        if was_created:
            created += 1
        else:
            updated += 1

        # keep basics in sync
        if cat_obj:
            item.category_fk = cat_obj
        qty = avail.get("quantity", item.quantity)
        item.quantity = qty
        # An explicit availability flag may represent an untracked catalog.
        item.is_available = avail.get("is_available", qty > 0)

        # don't clobber a rich description if already set; only set if empty
        if not (item.description or "").strip():
            item.description = (row.get("flavor_profile") or "")[:1000]

        # Merge a few helpful keys into meta (still freeform)
        meta = dict(item.meta or {})
        for k in ("recommendations", "specialty_items", "source_quality", "pairings", "ingredients"):
            if k in row and row[k] is not None:
                meta[k] = row[k]
        if 'knowledge' in data:
            meta['catalog_listing'] = {
                'listed_variant': row['listed_variant'].strip(),
                'listed_size': row.get('listed_size'),
            }
        item.meta = meta
        item.full_clean(exclude=["platform_item_ids"])
        item.save()
        imported_ids.append(item.pk)

        # Per-item catalog meta (ONE per MenuItem).
        # IMPORTANT: MenuCatalogMeta has NO 'tenant' field; relate via menu_item only.
        catmeta, _ = MenuCatalogMeta.objects.get_or_create(menu_item=item)

        # JSON coercers / normalizers
        # Normalize booleans inside dict-like sections that often come as "True"/"False" strings.
        dp = row.get("dietary_preferences")
        al = row.get("allergens")

        if isinstance(dp, dict):
            catmeta.dietary_preferences = _normalize_bool_dict(dp)
        else:
            catmeta.dietary_preferences = _to_json(dp, {})

        if isinstance(al, dict):
            catmeta.allergens = _normalize_bool_dict(al)
        else:
            catmeta.allergens = _to_json(al, {})

        prep = row.get("preparation")
        if isinstance(prep, str):
            catmeta.preparation = {"text": prep}
        else:
            catmeta.preparation = _to_json(prep, {})

        catmeta.nutrition = _to_json(row.get("nutrition"), {})
        catmeta.ingredients = _to_json(row.get("ingredients"), [])
        catmeta.recommendations = _to_json(row.get("recommendations"), [])
        catmeta.specialty_items = _to_json(row.get("specialty_items"), [])
        catmeta.source_quality = _to_json(row.get("source_quality"), [])
        catmeta.pairings = _to_json(row.get("pairings"), [])

        eo = row.get("explore_options")
        if isinstance(eo, (bool, int, float, str)):
            catmeta.explore_options = {"enabled": _parse_bool(eo)}
        else:
            catmeta.explore_options = _to_json(eo, {})

        # flavor_profile is stored as plain text on the model per your field list
        fp = row.get("flavor_profile")
        if isinstance(fp, str) and fp.strip():
            catmeta.flavor_profile = fp

        catmeta.save()

        # Variants from pricing + portion_and_size
        pricing = row.get("pricing") or {}
        portions = row.get("portion_and_size") or {}

        # dessert case: top-level dict with weight_grams/description
        dessert_weight = portions.get("weight_grams") if isinstance(portions, dict) else None
        dessert_desc = portions.get("description") if isinstance(portions, dict) and isinstance(portions.get("description"), str) else None

        for position, (label, price_str) in enumerate(pricing.items()):
            size = str(label).strip()
            if not size:
                continue
            amount = parse_price(price_str)

            v = item.variants.filter(size__iexact=size).order_by('-is_available', 'pk').first()
            if not v:
                v = MenuItemVariant(menu_item=item, size=size, price=amount)
                v_added += 1
            else:
                v.price = amount
                v_updated += 1

            v.sort_order = position
            v.size = size
            v.is_available = True

            # attach volumes/weights/desc
            p = portions.get(label) if isinstance(portions, dict) and isinstance(portions.get(label), dict) else None
            if p:
                v.volume_ml = p.get("volume_ml")
                v.weight_grams = p.get("weight_grams")
                if p.get("description"):
                    v.description = p.get("description")

            # For desserts with per_quantity, use the top-level weight/description if present
            if size == "per_quantity":
                if dessert_weight is not None:
                    v.weight_grams = dessert_weight
                if dessert_desc and not v.description:
                    v.description = dessert_desc

            v.full_clean()
            v.save()
    knowledge = _catalog_knowledge(tenant, data, imported_ids) if 'knowledge' in data else generate_menu_items_json(tenant)
    import_documents(tenant, 'knowledge', knowledge)
    return created, updated, v_added, v_updated
