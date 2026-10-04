"""Catalog ownership and atomic imports into the existing restaurant tables."""
from datetime import timedelta
import hashlib
import json
import uuid

from django.db import transaction
from django.utils import timezone
from chatbot_core.models import TenantInfo
from orders.models import MenuItem, MenuItemVariant, MenuCategory, AddonGroup, AddonItem, ItemAddonGroup
from .models import MenuSource, ExternalMapping, Configuration, Connection
from .menu_schema import MenuSnapshot


def lock_menu(tenant_id):
    # Also protects the implicit-local case where no MenuSource row exists yet.
    TenantInfo.objects.select_for_update().get(pk=tenant_id)


def source_for(tenant_id):
    return MenuSource.objects.select_related('connection__location').filter(tenant_id=tenant_id).first()


def assert_local_menu(tenant_id):
    if MenuSource.objects.filter(tenant_id=tenant_id, mode='external').exists():
        raise ValueError('This menu is managed externally. Update it in the connected menu application.')


def assert_menu_fresh(tenant_id):
    source = source_for(tenant_id)
    if not source or source.mode == 'local':
        return
    connection = source.connection
    config = Configuration.objects.filter(tenant_id=tenant_id).first()
    currency = config.policy['currency'] if config else 'INR'
    if (not connection or not connection.active or connection.role != 'pos'
            or connection.location.tenant_id != source.tenant_id or 'catalog.write' not in connection.capabilities
            or source.currency != currency or not source.observed_at or not source.synced_at
            or timezone.now() - source.observed_at > timedelta(seconds=source.max_age_seconds)):
        raise ValueError('Ordering is temporarily unavailable while we refresh the restaurant menu. Please try again shortly.')


@transaction.atomic
def configure_source(tenant_id, *, mode, connection=None, max_age_seconds=900):
    lock_menu(tenant_id)
    source, _ = MenuSource.objects.get_or_create(tenant_id=tenant_id)
    changed = source.mode != mode or source.connection_id != (connection.pk if connection else None)
    source.mode, source.connection, source.max_age_seconds = mode, connection, max_age_seconds
    source.full_clean()
    if changed:
        source.generation = uuid.uuid4()
        source.sequence, source.revision, source.payload_hash, source.currency = 0, '', '', ''
        source.observed_at = source.synced_at = None
    source.save()
    return source


@transaction.atomic
def import_snapshot(connection, payload):
    snapshot = MenuSnapshot.model_validate(payload)
    tenant_id = connection.location.tenant_id
    lock_menu(tenant_id)
    connection = Connection.objects.select_related('location').get(pk=connection.pk)
    source = source_for(tenant_id)
    if (not source or source.mode != 'external' or source.connection_id != connection.pk
            or not connection.active or connection.role != 'pos' or 'catalog.write' not in connection.capabilities):
        raise ValueError('This connection is not the configured menu authority.')
    if snapshot.source_generation != source.generation:
        raise ValueError('Menu source changed. Fetch the manifest before sending a new snapshot.')
    encoded = json.dumps(snapshot.model_dump(mode='json'), sort_keys=True, separators=(',', ':'))
    payload_hash = hashlib.sha256(encoded.encode()).hexdigest()
    if snapshot.sequence == source.sequence:
        if source.payload_hash != payload_hash:
            raise ValueError('Sequence already used for a different snapshot.')
        # A replay never refreshes the catalog's observation time.
        return {'status': 'unchanged', 'sequence': source.sequence}
    if snapshot.sequence < source.sequence:
        raise ValueError('An older menu snapshot cannot replace the current catalog.')
    now = timezone.now()
    if (snapshot.observed_at > now + timedelta(seconds=30)
            or now - snapshot.observed_at > timedelta(seconds=source.max_age_seconds)
            or (source.observed_at and snapshot.observed_at < source.observed_at)):
        raise ValueError('Menu observation is stale, out of order, or in the future.')
    config = Configuration.objects.filter(tenant_id=tenant_id).first()
    if snapshot.currency != (config.policy['currency'] if config else 'INR'):
        raise ValueError('Menu currency must match the tenant currency; prices are never converted.')

    def upsert(kind, external_id, model, ownership, values, scope=''):
        mapping = ExternalMapping.objects.filter(connection=connection, kind=kind, scope=scope, external_id=external_id).first()
        if mapping:
            row = model.objects.filter(pk=mapping.canonical_id, **ownership).first()
            if row is None:
                raise ValueError('Menu mapping refers to a missing entity or a different tenant/parent.')
            for key, value in values.items():
                setattr(row, key, value)
            row.save()
            mapping.revision = snapshot.revision
            mapping.save(update_fields=['revision'])
        else:
            # Categories already have a tenant-wide unique name; adopting an
            # unmapped local category avoids duplicate labels on first import.
            row = model.objects.filter(**ownership, name__iexact=values['name']).first() if kind == 'category' else None
            if row is not None:
                if ExternalMapping.objects.filter(connection=connection, kind=kind, scope=scope, canonical_id=str(row.pk)).exists():
                    raise ValueError('Category name is already used by a different external identity.')
                for key, value in values.items():
                    setattr(row, key, value)
                row.save()
            else:
                row = model.objects.create(**ownership, **values)
            ExternalMapping.objects.create(connection=connection, kind=kind, scope=scope,
                external_id=external_id, canonical_id=str(row.pk), revision=snapshot.revision)
        return row

    # Disable instead of deleting: order and stock references must survive.
    MenuItem.objects.filter(tenant_id=tenant_id).update(is_available=False)
    MenuItemVariant.objects.filter(menu_item__tenant_id=tenant_id).update(is_available=False)
    AddonItem.objects.filter(group__tenant_id=tenant_id).update(is_available=False)
    categories, groups = {}, {}
    for category in snapshot.categories:
        categories[category.external_id] = upsert('category', category.external_id, MenuCategory,
            {'tenant_id': tenant_id}, {'name': category.name.strip(), 'sort_order': category.sort_order, 'is_active': category.available})
    for group in snapshot.modifier_groups:
        row = upsert('modifier_group', group.external_id, AddonGroup, {'tenant_id': tenant_id}, {'name': group.name})
        groups[group.external_id] = row
        for option in group.options:
            upsert('modifier', option.external_id, AddonItem, {'group_id': row.pk},
                {'name': option.name, 'price': option.price, 'is_available': option.available,
                 'min_quantity': option.min_quantity, 'max_quantity': option.max_quantity}, scope=group.external_id)
    for item in snapshot.items:
        row = upsert('item', item.external_id, MenuItem, {'tenant_id': tenant_id},
            {'name': item.name, 'description': item.description, 'is_available': item.available,
             'category_fk': categories.get(item.category_id), 'quantity': 0})
        variants = {}
        for variant in item.variants:
            variants[variant.external_id] = upsert('variant', variant.external_id, MenuItemVariant,
                {'menu_item_id': row.pk}, {'size': variant.name.strip(), 'price': variant.price,
                 'is_available': variant.available, 'sort_order': variant.sort_order}, scope=item.external_id)
        keep_groups = []
        for rule in item.modifier_groups:
            group = groups[rule.group_id]
            keep_groups.append(group.pk)
            ItemAddonGroup.objects.update_or_create(tenant_id=tenant_id, item=row, group=group,
                defaults={'min_selections': rule.min_selections, 'max_selections': rule.max_selections,
                          'variant_ids': [str(variants[v].pk) for v in rule.variant_ids]})
        row.addon_groups.exclude(group_id__in=keep_groups).delete()
    source.sequence, source.revision = snapshot.sequence, snapshot.revision
    source.payload_hash, source.observed_at, source.synced_at = payload_hash, snapshot.observed_at, now
    source.currency = snapshot.currency
    source.save()
    # Customer-facing menu readers query these tables each turn; no static
    # knowledge publication or external cache operation is required to commit.
    return {'status': 'applied', 'sequence': source.sequence, 'items': len(snapshot.items)}
