"""Validate and atomically publish drafts; read versions from the shared database.

Workers poll on each turn/read. No notification delivery or process restart is
needed for correctness. A turn owns a consistent bundle, never a session version.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import re
from threading import RLock

from django.core.exceptions import ValidationError
from django.apps import apps
from django.db import transaction
from django.utils import timezone

from chatbot_core.capabilities import CAPABILITIES, CHECKOUT_TOPICS, CONTROL_ROUTES
from chatbot_core.models import TenantInfo, TenantJSONDoc, TenantRuntimeConfiguration

_active = ContextVar("runtime_configuration", default=None)
_turn_menus = ContextVar("runtime_turn_menus", default=None)
_cache = OrderedDict()
_cache_lock = RLock()
MAX_CACHED_TENANTS = 256
NAME = re.compile(r"[a-z][a-z0-9_]{0,119}\Z")


def present(value):
    return value is not None and value != {} and value != [] and value != "" and (not isinstance(value, str) or bool(value.strip()))


def classification_options(payload):
    if isinstance(payload, str):
        return {"description": payload, "enabled": True}
    return payload if isinstance(payload, dict) else {}


def validate_documents(tenant, documents, *, registry=apps, using='default'):
    """Validate the entire candidate before changing its published version."""
    errors, indexed = [], {}
    limits_ready = None
    allowed_types = set(TenantJSONDoc.DocType.values)
    for doc in documents:
        dtype, intent, topic = (doc.get(key) for key in ("dtype", "intent", "sub_intent"))
        label = f"{dtype}/{intent}/{topic}"
        if dtype not in allowed_types or not all(isinstance(v, str) and NAME.fullmatch(v) for v in (intent, topic)):
            errors.append(f"{label}: use a valid document type and lowercase topic names.")
            continue
        if intent not in CAPABILITIES:
            errors.append(f"{label}: capability is not implemented.")
        key = (dtype, intent, topic)
        if key in indexed:
            errors.append(f"{label}: duplicate document.")
        indexed[key] = doc.get("payload")

    for (dtype, intent, topic), payload in indexed.items():
        if dtype != "intent_classification" or intent not in CAPABILITIES:
            continue
        label = f"{intent}/{topic}"
        capability = CAPABILITIES[intent]
        if not capability.supports(topic):
            errors.append(f"{label}: this executable route is not implemented.")
        options = classification_options(payload)
        if not isinstance(payload, (str, dict)) or not options:
            errors.append(f"{label}: provide a description or a classification object.")
            continue
        allowed = {"description", "enabled", "examples", "required_settings", "required_knowledge", "catalog_references"}
        if options.keys() - allowed:
            errors.append(f"{label}: unknown classification fields: {', '.join(sorted(options.keys() - allowed))}.")
        if type(options.get("enabled", True)) is not bool:
            errors.append(f"{label}: enabled must be true or false.")
        for field in ("examples", "required_settings", "required_knowledge", "catalog_references"):
            value = options.get(field, [])
            if not isinstance(value, list):
                errors.append(f"{label}: {field} must be a list.")
        if options.get("enabled", True) is False:
            continue
        if not isinstance(options.get("description"), str) or not options["description"].strip():
            errors.append(f"{label}: a description is required.")
        examples = options.get("examples", [])
        if isinstance(examples, list) and any(not isinstance(v, str) or not v.strip() for v in examples):
            errors.append(f"{label}: examples must be nonempty strings.")
        instructions = indexed.get(("response_intents", intent, topic))
        if not isinstance(instructions, str) or not instructions.strip():
            errors.append(f"{label}: response instructions are required.")
        required_knowledge = options.get("required_knowledge", [])
        if not isinstance(required_knowledge, list):
            required_knowledge = []
        else:
            required_knowledge = list(required_knowledge)
        if capability.requires_knowledge:
            required_knowledge.append(f"{intent}/{topic}")
        for reference in required_knowledge:
            parts = reference.split("/") if isinstance(reference, str) else []
            if len(parts) != 2 or not present(indexed.get(("knowledge", *parts))):
                errors.append(f"{label}: required knowledge {reference!r} is missing.")
        required_settings = options.get("required_settings", [])
        for reference in required_settings if isinstance(required_settings, list) else []:
            value = tenant.meta or {}
            for part in reference.split(".") if isinstance(reference, str) and reference else [None]:
                value = value.get(part) if isinstance(value, dict) else None
            if not present(value):
                errors.append(f"{label}: required tenant setting {reference!r} is missing.")
        if intent == "placing_order" and classification_options(payload).get("enabled", True) is True:
            if limits_ready is None:
                limits_ready = ordering_limits_ready(tenant.pk, using=using)
            if not limits_ready:
                errors.append(f"{label}: set quantity and amount limits before enabling ordering.")
        if intent == "placing_order" and topic in CHECKOUT_TOPICS:
            CheckoutSettings = registry.get_model('orders', 'CheckoutSettings')
            from orders.checkout_config import validate_checkout_config
            config = CheckoutSettings.objects.using(using).filter(tenant_id=tenant.pk).values_list("configuration", flat=True).first()
            try:
                if config is None:
                    raise ValidationError("Configure checkout in Settings first.")
                validate_checkout_config(config)
            except ValidationError as exc:
                errors.extend(f"{label}: {message}" for message in exc.messages)
        references = options.get("catalog_references", [])
        for reference in references if isinstance(references, list) else []:
            if not valid_catalog_reference(tenant.pk, reference, registry=registry, using=using):
                errors.append(f"{label}: catalog reference {reference!r} does not belong to this cafe or no longer exists.")
    if errors:
        raise ValidationError(errors)


def ordering_limits_ready(tenant_id, *, using='default') -> bool:
    """Ordering routes stay disabled until this tenant saved explicit limits."""
    from commerce.models import Configuration
    from commerce.policy import Policy
    row = Configuration.objects.using(using).filter(tenant_id=tenant_id).values_list("policy", flat=True).first()
    if row is None:
        return False
    try:
        return Policy.model_validate(row).ordering_limits is not None
    except ValueError:
        return False


def valid_catalog_reference(tenant_id, reference, *, registry=apps, using='default'):
    MenuItem, MenuCategory, MenuItemVariant, AddonItem = (
        registry.get_model('orders', name) for name in ('MenuItem', 'MenuCategory', 'MenuItemVariant', 'AddonItem'))
    models = {"item": (MenuItem, "tenant_id"), "category": (MenuCategory, "tenant_id"),
              "variant": (MenuItemVariant, "menu_item__tenant_id"), "addon": (AddonItem, "group__tenant_id")}
    if not isinstance(reference, dict) or set(reference) != {"type", "id"}:
        return False
    if not isinstance(reference["type"], str) or reference["type"] not in models or not isinstance(reference["id"], (str, int)) or isinstance(reference["id"], bool):
        return False
    model, tenant_field = models[reference["type"]]
    try:
        return model.objects.using(using).filter(pk=reference["id"], **{tenant_field: tenant_id}).exists()
    except (ValueError, TypeError, ValidationError):
        return False


@transaction.atomic
def publish_configuration(tenant_id, *, expected_version):
    # Lock the tenant too: publishing a tenant's first version must serialize.
    tenant = TenantInfo.objects.select_for_update().get(pk=tenant_id)
    publication, _ = TenantRuntimeConfiguration.objects.get_or_create(tenant=tenant)
    if type(expected_version) is not int or expected_version != publication.version:
        raise ValidationError("The published version changed. Reload the page before publishing.")
    documents = list(TenantJSONDoc.objects.filter(tenant=tenant).order_by("dtype", "intent", "sub_intent")
                     .values("dtype", "intent", "sub_intent", "payload"))
    validate_documents(tenant, documents)
    publication.documents = documents
    publication.version += 1
    publication.published_at = timezone.now()
    publication.save(update_fields=["documents", "version", "published_at"])
    return publication


@transaction.atomic
def publish_default_configuration(tenant_id):
    """Give a new cafe validated conversational and basket capabilities."""
    tenant = TenantInfo.objects.select_for_update().get(pk=tenant_id)
    if TenantRuntimeConfiguration.objects.filter(tenant=tenant, version__gt=0).exists():
        return tenant.runtime_configuration
    topics = {'general': CAPABILITIES['general'].sub_intents,
              'insufficient_information': {'insufficient_information'},
              'out_of_context': {'out_of_scope'},
              'placing_order': {'initiate_order', 'add_to_basket', 'update_order', 'delete_entry',
                                'check_order_cart', 'customize_confirmation', 'insufficient_information_order'}}
    for intent, names in topics.items():
        for topic in sorted(names):
            for dtype, payload in (
                    ('intent_classification', {
                        'description': topic.replace('_', ' '),
                        # Ordering stays off until a tenant policy sets explicit limits.
                        'enabled': intent != 'placing_order',
                    }),
                    ('response_intents', 'Help with this request using available cafe information. Ask for clarification when needed.')):
                TenantJSONDoc.objects.get_or_create(tenant=tenant, dtype=dtype, intent=intent,
                                                   sub_intent=topic, defaults={'payload': payload})
    return publish_configuration(tenant.pk, expected_version=0)


@dataclass(frozen=True)
class RuntimeConfiguration:
    tenant_id: str
    api_key: str
    slug: str
    version: int
    documents: list

    @property
    def published(self):
        return self.version > 0

    def allows(self, intent, topic):
        if (intent, topic) in CONTROL_ROUTES:
            return True
        if not self.published:
            return False
        capability = CAPABILITIES.get(intent)
        if capability is None or not capability.supports(topic):
            return False
        return any(doc["dtype"] == "intent_classification" and doc["intent"] == intent
                   and doc["sub_intent"] == topic
                   and classification_options(doc["payload"]).get("enabled", True) is True
                   for doc in self.documents)

    def document(self, dtype, intent, topic):
        if not self.published:
            return None
        if dtype == 'knowledge' and intent == 'menu_items':
            from commerce.menu_sync import source_for, assert_menu_fresh
            source = source_for(self.tenant_id)
            if source and source.mode == 'external':
                from users.utils import generate_menu_items_json
                try:
                    assert_menu_fresh(int(self.tenant_id))
                    payload = generate_menu_items_json(source.tenant)['menu_items'].get(topic, {})
                    payload = {'topic': payload, 'catalog': current_menu(self.api_key) or {},
                               'observed_at': source.observed_at.isoformat()}
                except ValueError as exc:
                    payload = {'menu_status': str(exc)}
                return {'slug': self.slug, 'intent': intent, 'payload': payload,
                        'identity': (self.tenant_id, dtype, intent, topic, self.version,
                                     str(source.generation), source.sequence)}
        entry = None
        for doc in self.documents:
            if (doc["dtype"], doc["intent"], doc["sub_intent"]) == (dtype, intent, topic):
                entry = {"slug": self.slug, "intent": intent, "payload": deepcopy(doc["payload"]),
                         "identity": (self.tenant_id, dtype, intent, topic, self.version)}
                break
        if dtype == "knowledge":
            # Required knowledge is also supplied to the answerer. Read raw
            # documents so references cannot recurse or cross tenant boundaries.
            classification = next((doc for doc in self.documents if
                (doc["dtype"], doc["intent"], doc["sub_intent"]) == ("intent_classification", intent, topic)), None)
            options = classification_options(classification["payload"]) if classification else {}
            references = options.get("required_knowledge", [])
            dependencies = {f'{doc["intent"]}/{doc["sub_intent"]}': deepcopy(doc["payload"])
                            for doc in self.documents if doc["dtype"] == "knowledge"
                            and f'{doc["intent"]}/{doc["sub_intent"]}' in references}
            if dependencies:
                entry = entry or {"slug": self.slug, "intent": intent,
                                  "identity": (self.tenant_id, dtype, intent, topic, self.version)}
                entry["payload"] = {"topic": entry.get("payload"), "required_knowledge": dependencies}
        return entry


class ClassificationSchema(dict):
    def __init__(self, *args, version=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.version = version


def active_configuration():
    return _active.get()


def current_menu(api_key):
    """Load live catalog data once per turn; checkout still validates the DB."""
    from chatbot_core.knowledge_cache import generate_all_menu_payload
    menus = _turn_menus.get()
    if menus is None:
        return generate_all_menu_payload(api_key=api_key).get(api_key)
    if api_key not in menus:
        menus[api_key] = generate_all_menu_payload(api_key=api_key).get(api_key)
    return deepcopy(menus[api_key])


def get_configuration(*, tenant_id=None, api_key=None):
    active = _active.get()
    if active and ((tenant_id is not None and active.tenant_id == str(tenant_id)) or
                   (api_key is not None and active.api_key == api_key)):
        return active
    filters = {"pk": tenant_id} if tenant_id is not None else {"api_key": api_key}
    if tenant_id is not None and not str(tenant_id).isdigit():
        return None
    # Check the shared version even on a local cache hit; pub/sub is unnecessary.
    row = TenantInfo.objects.filter(**filters).values("pk", "api_key", "slug", "runtime_configuration__version").first()
    if row is None:
        return None
    version = row["runtime_configuration__version"] or 0
    identity = (str(row["pk"]), row["api_key"], row["slug"], version)
    with _cache_lock:
        cached = _cache.get(identity)
        if cached is not None:
            _cache.move_to_end(identity)
            return deepcopy(cached)
    publication = TenantRuntimeConfiguration.objects.filter(tenant_id=row["pk"]).values("version", "documents").first()
    # Read version and payload together on a miss, including a concurrent publish.
    configuration = RuntimeConfiguration(str(row["pk"]), row["api_key"], row["slug"],
        publication["version"] if publication else 0, publication["documents"] if publication else [])
    identity = (configuration.tenant_id, configuration.api_key, configuration.slug, configuration.version)
    with _cache_lock:
        for key in list(_cache):
            if key[0] == configuration.tenant_id:
                del _cache[key]
        _cache[identity] = configuration
        while len(_cache) > MAX_CACHED_TENANTS:
            _cache.popitem(last=False)
    return deepcopy(configuration)


@contextmanager
def configuration_for_turn(tenant_id):
    # Always fetch the shared version at the next turn, including an old session.
    token = _active.set(None)
    menu_token = _turn_menus.set({})
    try:
        _active.set(get_configuration(tenant_id=tenant_id))
        yield
    finally:
        _turn_menus.reset(menu_token)
        _active.reset(token)
