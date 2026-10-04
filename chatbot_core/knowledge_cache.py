from orders.models import MenuItem, MenuItemVariant
from collections import OrderedDict, defaultdict
from decimal import Decimal
from django.db.models import Prefetch, Q
from chatbot_core.scope import required_identity


class DocumentCache:
    """Compatibility accessor; lookups require a complete document path."""
    def __init__(self, dtype):
        self.dtype = dtype

    def get(self, key, default=None):
        from chatbot_core.runtime_configuration import get_configuration
        api_key, intent, topic = key
        configuration = get_configuration(api_key=api_key)
        if configuration is None:
            return default
        return configuration.document(self.dtype, intent, topic) or default

    def clear(self):
        from chatbot_core.runtime_configuration import _cache, _cache_lock
        from chatbot_core.knowledge_retrieval import _indexes, _lock
        with _cache_lock:
            _cache.clear()
        with _lock:
            _indexes.clear()


_INTENT_PROMPT_CACHE = DocumentCache("response_intents")
_KNOWLEDGE_BASE_CACHE = DocumentCache("knowledge")
_INTENT_CLASSIFICATION_CACHE = {}
_ITEM_PRICING_CACHE = {}


class LiveMenuCache:
    """Catalog availability and pricing are refreshed for every turn."""
    def get(self, api_key, default=None):
        from chatbot_core.runtime_configuration import current_menu
        menu = current_menu(api_key)
        return menu if menu is not None else default

    def __getitem__(self, api_key):
        menu = self.get(api_key)
        if menu is None:
            raise KeyError(api_key)
        return menu

    def clear(self):
        _ITEM_PRICING_CACHE.clear()


def get_intent_prompt_cache():
    return _INTENT_PROMPT_CACHE


def get_knowledge_base_cache():
    return _KNOWLEDGE_BASE_CACHE


def get_intent_classification_cache(tenant_id):
    from chatbot_core.runtime_configuration import get_configuration, ClassificationSchema, classification_options
    configuration = get_configuration(tenant_id=required_identity(tenant_id, "tenant_id"))
    schema = ClassificationSchema(version=configuration.version if configuration else 0)
    if configuration:
        for doc in configuration.documents:
            if doc["dtype"] == "intent_classification" and configuration.allows(doc["intent"], doc["sub_intent"]):
                options = classification_options(doc["payload"])
                schema.setdefault(doc["intent"], {})[doc["sub_intent"]] = {
                    "description": options.get("description", ""), "examples": options.get("examples", []),
                }
    return schema


def get_item_pricing_cache():
    return LiveMenuCache()


def load_intent_classification_cache():
    # Lazy readers check the database version; startup does not freeze a schema.
    _INTENT_CLASSIFICATION_CACHE.clear()


def load_knowledge_cache(dtype):
    return DocumentCache(dtype)


def _price_to_string(value: Decimal) -> str:
    s = format(value.normalize(), 'f')
    return s if s else "0"

def _description_for(item: MenuItem) -> str:
    cat = getattr(item, "catalog_meta", None)
    return (item.description or (cat.flavor_profile if cat else "") or "").strip()

def _tags_for(item: MenuItem):
    # Prefer explicit tags in MenuItem.meta["tags"]; else derive a few from CatalogMeta.ingredients
    if isinstance(item.meta, dict):
        tags = item.meta.get("tags")
        if isinstance(tags, list):
            return [str(t) for t in tags]
    cat = getattr(item, "catalog_meta", None)
    if cat and isinstance(cat.ingredients, list):
        return [str(x).strip().lower() for x in cat.ingredients[:3]]
    return []

def generate_all_menu_payload(api_key=None) -> dict:
    """
    Returns:
      {
        "<tenant_api_key_A>": [ ...items... ],
        "<tenant_api_key_B>": [ ...items... ],
        ...
      }
    """
    # Pull all available items in one go, with their tenant and related data
    items_qs = (
        MenuItem.objects
        .filter(is_available=True)
        .filter(Q(category_fk__isnull=True) | Q(category_fk__is_active=True))
        .select_related("tenant", "category_fk")
        .prefetch_related(
            Prefetch("variants", queryset=MenuItemVariant.objects.filter(is_available=True)),
            "catalog_meta", "addon_groups__group__addons",
        )
        .order_by("tenant__id", "category_fk__sort_order", "category_fk__name", "name")
    )
    if api_key is not None:
        items_qs = items_qs.filter(tenant__api_key=api_key)
    from commerce.models import MenuSource
    sources = MenuSource.objects.filter(mode='external')
    if api_key is not None:
        sources = sources.filter(tenant__api_key=api_key)
    external_tenants = set(sources.values_list('tenant_id', flat=True))
    result = defaultdict(dict)
    for item in items_qs:
        variants = item.variants.all()
        pricing = OrderedDict()
        item_variant_map = OrderedDict()
        # Use the canonical variant UUID as the key
        for v in variants:
            key = str(v.id)
            pricing[key] = _price_to_string(v.price)
            item_variant_map[v.size] = key
        item_name = item.name
        quantity = None if item.tenant_id in external_tenants else item.quantity
        from chatbot_core.logic.cafe.catalog import serialize_item
        result[item.tenant.api_key][item_name] = {
            **serialize_item(item),
            "item_id": str(item.id),
            "name": item_name,
            "category_id": item.category_fk_id,
            "category": item.category_fk.name if item.category_fk else "Uncategorized",
            "pricing": pricing,
            "item_variant_map": item_variant_map,
            "description": _description_for(item),
            "tags": _tags_for(item),
            "available_quantity": quantity,
        }
    # Convert defaultdict to regular dict for JSON-serializability
    return dict(result)

def initialize_caches():
    """Clear this process's optional hot copies. Other workers poll DB versions."""
    _KNOWLEDGE_BASE_CACHE.clear()
    _ITEM_PRICING_CACHE.clear()
    _INTENT_CLASSIFICATION_CACHE.clear()
