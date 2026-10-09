from __future__ import annotations
from django.db.models import Func

from collections import OrderedDict
from decimal import Decimal
from typing import Dict, Any, List

from django.db.models import Prefetch, Q

from orders.models import MenuItem, MenuItemVariant, MenuCatalogMeta

INR = "₹"

class ExtractEpoch(Func):
    function = 'EXTRACT'
    template = "%(function)s(EPOCH FROM %(expressions)s)"

def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) > 0
    if isinstance(v, (int, float, Decimal)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)

def _prep_text(prep) -> str:
    if isinstance(prep, str):
        return prep
    if isinstance(prep, dict):
        for k in ("text", "value", "desc", "description"):
            if isinstance(prep.get(k), str):
                return prep[k]
    return ""

def _nutrition_normalized(nu: dict | None) -> dict:
    nu = dict(nu or {})
    nu.setdefault("serving_size", "per 100g")
    nu.setdefault("note", "Values are approximate and can vary slightly by flavor. ")
    # coerce number-like strings
    for k in ("calories_kcal","total_fat_g","saturated_fat_g","carbohydrates_g","sugars_g","protein_g"):
        if k in nu and isinstance(nu[k], str):
            try:
                nu[k] = float(nu[k]) if "." in nu[k] else int(nu[k])
            except Exception:
                pass
    return nu

def _fmt_price(v: Decimal | float | int | None, currency='INR') -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        # drop trailing .00
        q = v.quantize(Decimal("1")) if v == v.to_integral() else v.normalize()
        s = f"{q}"
    else:
        s = str(v)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return f"{INR}{s}" if currency == 'INR' else f"{currency} {s}"

def generate_menu_items_json(tenant) -> Dict[str, Any]:
    """
    Build the 'menu_items' JSON for the given tenant.

    Pulls:
    - Availability from MenuItem.quantity
    - Recommendations / dietary_preferences / allergens / ingredients / nutrition /
      preparation / specialty_items / source_quality / pairings / flavor_profile /
      explore_options from MenuCatalogMeta JSON fields (when present)
    - Pricing & portion_and_size from MenuItemVariant (size/price/volume_ml/weight_grams/description)
    - menu_category from MenuCategory.name

    Returns a dict with a single key "menu_items" that matches your target schema.
    """
    from commerce.models import MenuSource, Configuration
    external = MenuSource.objects.filter(tenant=tenant, mode='external').exists()
    config = Configuration.objects.filter(tenant=tenant).first()
    currency = config.policy['currency'] if config else 'INR'
    # Load all items for tenant with related data in minimum queries
    items_qs = (
        MenuItem.objects
        .filter(tenant=tenant, is_available=True)
        .filter(Q(category_fk__isnull=True) | Q(category_fk__is_active=True))
        .select_related("category_fk")
        .prefetch_related(
            Prefetch("variants", queryset=MenuItemVariant.objects.filter(is_available=True)),
            Prefetch("catalog_meta", queryset=MenuCatalogMeta.objects.all())
        )
        .order_by("category_fk__sort_order", "category_fk__name", "name")
    )

    # --- Initialize all top-level buckets with stable ordering where it matters ---
    availability: List[Dict[str, Dict[str, int]]] = []
    recommendations: Dict[str, List[str]] = OrderedDict()
    # we will finalize these two from aggregation buckets later
    dietary_preferences: Dict[str, Any] = OrderedDict()
    allergens: Dict[str, Any] = OrderedDict()

    ingredients: Dict[str, Any] = OrderedDict()
    nutrition: Dict[str, Any] = OrderedDict()
    preparation: Dict[str, Any] = OrderedDict()
    pricing: Dict[str, Dict[str, str]] = OrderedDict()
    specialty_items: Dict[str, List[str]] = OrderedDict()
    pairings: Dict[str, List[str]] = OrderedDict()
    source_quality: Dict[str, List[str]] = OrderedDict()
    flavor_profile: Dict[str, str] = OrderedDict()
    portion_and_size: Dict[str, Any] = OrderedDict()
    explore_options: Dict[str, Any] = OrderedDict()
    menu_category: Dict[str, str] = OrderedDict()

    # Aggregation buckets to match baseline shape
    dietary_buckets = OrderedDict([
        ("eggless_options", []),
        ("contains_dairy", []),
        ("nut_free_options", []),
        ("contains_nuts", []),
        ("vegan_options", []),
        ("gluten_free", []),
    ])
    allergen_buckets = OrderedDict([
        ("milk_and_dairy", []),
        ("tree_nuts", []),
        ("gluten", []),
        ("soy", []),
        ("eggs", []),
    ])

    # For notes
    dp_note = None
    al_note = None
    ing_note = None
    nutr_general_note = None
    prep_note = None
    pairings_note = None
    explore_note = None

    explore_items: List[str] = []

    for item in items_qs:
        item_name = item.name

        # availability
        availability.append({item_name: {"available": True} if external else {"quantity": int(item.quantity or 0)}})

        # category
        menu_category[item_name] = item.category_fk.name if item.category_fk else "Uncategorized"

        item_pricing = OrderedDict()
        item_portion = OrderedDict()
        for v in item.variants.all():
            item_pricing[v.size] = _fmt_price(v.price, currency)
            portion = {}
            if v.volume_ml is not None:
                portion["volume_ml"] = v.volume_ml
            if v.weight_grams is not None:
                portion["weight_grams"] = v.weight_grams
            if v.description:
                portion["description"] = v.description
            if portion:
                item_portion[v.size] = portion

        if item_pricing:
            pricing[item_name] = item_pricing
        if item_portion:
            portion_and_size[item_name] = item_portion

        # catalog meta (optional)
        meta: MenuCatalogMeta | None = getattr(item, "catalog_meta", None)
        if meta:
            # recommendations
            recs = meta.recommendations or []
            if recs:
                recommendations[item_name] = recs

            # specialty_items
            specs = meta.specialty_items or []
            if specs:
                specialty_items[item_name] = specs

            # source_quality
            sq = meta.source_quality or []
            if sq:
                source_quality[item_name] = sq

            # pairings (list) + possible note
            pr = meta.pairings or {}
            if isinstance(pr, dict):
                maybe_note = pr.get("note")
                if maybe_note and pairings_note is None:
                    pairings_note = maybe_note
                if item_name in pr and isinstance(pr[item_name], list):
                    pairings[item_name] = pr[item_name]
            elif isinstance(pr, list):
                if pr:
                    pairings[item_name] = pr

            # flavor_profile
            if meta.flavor_profile:
                flavor_profile[item_name] = meta.flavor_profile

            # ingredients + optional "note"
            ing = meta.ingredients or []
            if ing:
                ingredients[item_name] = ing
            if isinstance(meta.ingredients, dict) and meta.ingredients.get("note") and ing_note is None:
                ing_note = meta.ingredients.get("note")

            # nutrition + optional "general_note" (normalize per-item)
            nu = meta.nutrition or {}
            if isinstance(nu, dict) and any(k for k in nu.keys() if k not in {"general_note"}):
                nutrition[item_name] = _nutrition_normalized(nu)
            if isinstance(nu, dict) and "general_note" in nu and nutr_general_note is None:
                nutr_general_note = nu["general_note"]

            # preparation (unwrap {'text': '...'} → '...')
            prep_txt = _prep_text(meta.preparation)
            if prep_txt:
                preparation[item_name] = prep_txt
            if isinstance(meta.preparation, dict) and isinstance(meta.preparation.get("note"), str) and prep_note is None:
                prep_note = meta.preparation["note"]

            # dietary_preferences (per-item flags → global lists)
            dp = meta.dietary_preferences or {}
            if _truthy(dp.get("eggless_options")):
                dietary_buckets["eggless_options"].append(item_name)
            if _truthy(dp.get("contains_dairy")):
                dietary_buckets["contains_dairy"].append(item_name)
            if _truthy(dp.get("nut_free_options")):
                dietary_buckets["nut_free_options"].append(item_name)
            if _truthy(dp.get("contains_nuts")):
                dietary_buckets["contains_nuts"].append(item_name)
            if _truthy(dp.get("gluten_free")):
                dietary_buckets["gluten_free"].append(item_name)
            vo = dp.get("vegan_options")
            if isinstance(vo, (list, tuple, set)):
                dietary_buckets["vegan_options"].extend([str(x) for x in vo])
            elif _truthy(vo):
                dietary_buckets["vegan_options"].append(item_name)
            if isinstance(dp.get("note"), str) and dp_note is None:
                dp_note = dp["note"]

            # allergens (per-item flags → global lists)
            a = meta.allergens or {}
            if _truthy(a.get("milk_and_dairy")):
                allergen_buckets["milk_and_dairy"].append(item_name)
            if _truthy(a.get("tree_nuts")):
                allergen_buckets["tree_nuts"].append(item_name)
            if _truthy(a.get("gluten")):
                allergen_buckets["gluten"].append(item_name)
            if _truthy(a.get("eggs")):
                allergen_buckets["eggs"].append(item_name)
            if _truthy(a.get("soy")):
                allergen_buckets["soy"].append(item_name)
            if isinstance(a.get("note"), str) and al_note is None:
                al_note = a["note"]

            # explore_options (collect items + note)
            ex = meta.explore_options or {}
            if isinstance(ex, dict):
                lst = ex.get("items")
                if isinstance(lst, list):
                    explore_items.extend([x for x in lst if isinstance(x, str)])
                if ex.get("note") and explore_note is None:
                    explore_note = ex.get("note")

        # Always include item name for explore fallback
        explore_items.append(item_name)

    # De-duplicate while preserving order for explore items
    seen_names = set()
    unique_explore_items = []
    for n in explore_items:
        if n not in seen_names:
            unique_explore_items.append(n)
            seen_names.add(n)

    # Finalize dietary_preferences & allergens with notes
    dietary_preferences = dietary_buckets
    if dp_note:
        dietary_preferences["note"] = dp_note

    allergens = allergen_buckets
    if al_note:
        allergens["note"] = al_note

    # Ingredients global note already handled above (ing_note)
    if ing_note:
        ingredients["note"] = ing_note

    # Nutrition general note default if none captured
    if nutr_general_note:
        nutrition["general_note"] = nutr_general_note
    elif "general_note" not in nutrition:
        nutrition["general_note"] = "Nutrition information is estimated and intended for guidance only. Values may vary slightly depending on recipe variations and seasonal ingredients."

    # Preparation note if captured
    if prep_note and "note" not in preparation:
        preparation["note"] = prep_note

    # Pairings note if captured
    if pairings_note and "note" not in pairings:
        pairings["note"] = pairings_note

    # Final explore_options block
    explore_options["items"] = unique_explore_items
    if explore_note:
        explore_options["note"] = explore_note

    # Compose the final payload
    payload: Dict[str, Any] = {
        "menu_items": {
            "availability": availability,
            "recommendations": recommendations,
            "dietary_preferences": dietary_preferences,
            "allergens": allergens,
            "ingredients": ingredients,
            "nutrition": nutrition,
            "preparation": preparation,
            "pricing": pricing,
            "specialty_items": specialty_items,
            "pairings": pairings,
            "source_quality": source_quality,
            "flavor_profile": flavor_profile,
            "portion_and_size": portion_and_size,
            "explore_options": explore_options,
            "menu_category": menu_category,
        }
    }
    return payload


def publish_menu(tenant):
    """Regenerate draft chatbot knowledge from the already saved live catalog."""
    from django.db import transaction
    from chatbot_core.models import TenantJSONDoc
    from chatbot_core.knowledge_cache import initialize_caches

    data = generate_menu_items_json(tenant)
    with transaction.atomic():
        for sub_intent, payload in data["menu_items"].items():
            TenantJSONDoc.objects.update_or_create(
                tenant=tenant, dtype=TenantJSONDoc.DocType.KNOWLEDGE,
                intent="menu_items", sub_intent=sub_intent,
                defaults={"payload": payload},
            )
    initialize_caches()
