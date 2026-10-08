"""Shared parsing, validation and persistence for tenant configuration imports."""
from django.db import transaction
from chatbot_core.configuration_files import document_records, parse_json
from chatbot_core.models import TenantInfo, TenantJSONDoc
from orders.checkout_config import CheckoutPolicy, validate_checkout_config, validate_online_readiness


def validated_checkout(source, tenant):
    data = parse_json(source)
    validate_checkout_config(data)
    validate_online_readiness(data, tenant)
    return CheckoutPolicy.model_validate(data).model_dump(mode='json')


@transaction.atomic
def import_documents(tenant, dtype, source):
    records = document_records(dtype, source)
    TenantInfo.objects.select_for_update().get(pk=tenant.pk)
    created = updated = 0
    for record in records:
        payload = record.pop('payload')
        _, was_created = TenantJSONDoc.objects.update_or_create(
            tenant=tenant, **record, defaults={'payload': payload})
        created += was_created
        updated += not was_created
    return created, updated


@transaction.atomic
def import_checkout(tenant, source):
    from orders.models import CheckoutSettings
    TenantInfo.objects.select_for_update().get(pk=tenant.pk)
    configuration = validated_checkout(source, tenant)
    return CheckoutSettings.objects.update_or_create(tenant=tenant, defaults={'configuration': configuration})[0]


@transaction.atomic
def import_commerce_policy(tenant, source):
    from commerce.models import Configuration, Location
    from commerce.policy import Policy, validate_policy
    data = parse_json(source)
    validate_policy(data)
    policy = Policy.model_validate(data).model_dump(mode='json')
    TenantInfo.objects.select_for_update().get(pk=tenant.pk)
    configuration = Configuration.objects.filter(tenant=tenant).first()
    if configuration:
        configuration.policy = policy
        configuration.full_clean()
        configuration.save(update_fields=['policy'])
    else:
        location, _ = Location.objects.get_or_create(tenant=tenant, code='main', defaults={'name': tenant.display_name})
        configuration = Configuration.objects.create(tenant=tenant, location=location, enabled=False, policy=policy)
    return configuration


def import_configuration(tenant, kind, source):
    if kind == 'checkout':
        return import_checkout(tenant, source)
    if kind == 'commerce_policy':
        return import_commerce_policy(tenant, source)
    if kind == 'catalog':
        from orders.catalog_imports import import_catalog
        return import_catalog(tenant, source)
    return import_documents(tenant, kind, source)
