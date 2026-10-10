"""Atomic, non-destructive initialization and explicit ordering activation."""
from copy import deepcopy

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from chatbot_core.models import TenantInfo, TenantJSONDoc, TenantRuntimeConfiguration
from .models import CheckoutSettings, MenuItem
from .settings_defaults import ordering_defaults


@transaction.atomic
def initialize_ordering_settings(tenant, *, demo=False):
    """Fill missing records only. Existing policies, even absent limits, are intentional."""
    from commerce.models import Configuration, Location
    tenant = TenantInfo.objects.select_for_update().get(pk=tenant.pk)
    defaults = ordering_defaults(demo=demo)
    checkout, checkout_created = CheckoutSettings.objects.get_or_create(
        tenant=tenant, defaults={'configuration': defaults['checkout']})
    config = Configuration.objects.filter(tenant=tenant).first()
    config_created = config is None
    if config_created:
        location, _ = Location.objects.get_or_create(tenant=tenant, code='default', defaults={'name': tenant.display_name})
        config = Configuration.objects.create(tenant=tenant, location=location,
            policy=defaults['policy'])
    if checkout_created or config_created:
        tenant.meta = {**(tenant.meta or {}), 'ordering_setup_required': True}
        tenant.save(update_fields=['meta'])
    return checkout, config


@transaction.atomic
def complete_ordering_setup(tenant, configuration):
    """Save confirmed business details before publishing the complete ordering flow."""
    from chatbot_core.capabilities import CAPABILITIES
    from chatbot_core.configuration_imports import import_checkout
    from chatbot_core.runtime_configuration import validate_documents, ordering_limits_ready
    tenant = TenantInfo.objects.select_for_update().get(pk=tenant.pk)
    if not (tenant.meta or {}).get('ordering_setup_required'):
        raise ValidationError('Ordering setup is already complete. Use Checkout to edit your settings.')
    if not MenuItem.objects.filter(tenant=tenant, is_available=True, variants__is_available=True).filter(
            Q(category_fk__isnull=True) | Q(category_fk__is_active=True)).exists():
        raise ValidationError('Add at least one available menu item with a priced size before finishing setup.')
    if not ordering_limits_ready(tenant.pk):
        raise ValidationError('Save all six limits in Pricing & limits before finishing setup.')
    import_checkout(tenant, configuration)
    from commerce.models import Configuration
    from commerce.readiness import readiness_issues
    config = Configuration.objects.get(tenant=tenant)
    issues = readiness_issues(tenant, configuration=config, require_integrations=config.enabled)
    if issues:
        raise ValidationError(issues)
    config.local_checkout = True
    config.save(update_fields=['local_checkout'])
    tenant.meta = {**tenant.meta, 'ordering_setup_required': False}
    tenant.save(update_fields=['meta'])
    publication, _ = TenantRuntimeConfiguration.objects.get_or_create(tenant=tenant)
    # Only activate these routes; unrelated unpublished knowledge stays a draft.
    documents = {(d['dtype'], d['intent'], d['sub_intent']): deepcopy(d) for d in publication.documents}
    from users.utils import generate_menu_items_json
    menu = generate_menu_items_json(tenant)['menu_items']
    routes = {intent: CAPABILITIES[intent].sub_intents for intent in ('placing_order', 'location_based', 'order_enquiry')}
    routes['menu_items'] = {'pricing', 'explore_options', 'availability'}
    for intent, topics in routes.items():
        for topic in sorted(topics):
            records = [
                ('intent_classification', {'description': topic.replace('_', ' '), 'enabled': True}),
                ('response_intents', 'Help with this request using the saved menu, checkout details and order information. Ask for missing details.'),
            ]
            if intent == 'menu_items':
                records.append(('knowledge', menu[topic]))
            for dtype, payload in records:
                key = (dtype, intent, topic)
                document = documents.setdefault(key, dict(dtype=dtype, intent=intent, sub_intent=topic, payload=payload))
                if dtype == 'intent_classification':
                    value = document['payload']
                    document['payload'] = {**(value if isinstance(value, dict) else {'description': value}), 'enabled': True}
                draft, created = TenantJSONDoc.objects.get_or_create(tenant=tenant, dtype=dtype,
                    intent=intent, sub_intent=topic, defaults={'payload': document['payload']})
                if not created and dtype == 'intent_classification':
                    value = draft.payload
                    draft.payload = {**(value if isinstance(value, dict) else {'description': value}), 'enabled': True}
                    draft.save(update_fields=['payload'])
    candidate = list(documents.values())
    validate_documents(tenant, candidate)
    publication.documents = candidate
    publication.version += 1
    publication.published_at = timezone.now()
    publication.save(update_fields=['documents', 'version', 'published_at'])
    return publication
