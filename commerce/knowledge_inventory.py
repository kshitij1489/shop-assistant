"""Read-only inventory evidence for conversational answers, never reservations."""
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from orders.models import MenuItemVariant
from .menu_sync import assert_menu_fresh
from .models import Configuration, StockItem
from .policy import Policy


def _unknown(reason):
    return {'status': 'unknown', 'available_units': None, 'reason': reason}


def _stock_evidence(stock, policy, now):
    if stock is None:
        return _unknown('stock_not_configured')
    evidence = {
        'stock_pool_id': str(stock.pk),
        'source': 'provider' if stock.authority_id else 'local',
        'observed_at': stock.observed_at.isoformat() if stock.observed_at else None,
    }
    if stock.authority_id:
        authority = stock.authority
        if (authority.location_id != stock.location_id or not authority.active
                or 'inventory.update' not in authority.capabilities):
            return {**evidence, **_unknown('inventory_provider_unavailable')}
        if (not stock.observed_at
                or stock.observed_at < now - timedelta(seconds=policy.stock_max_age_seconds)
                or stock.observed_at > now + timedelta(seconds=60)):
            return {**evidence, **_unknown('stock_observation_missing_or_stale')}
    if not stock.available:
        return {**evidence, 'status': 'out_of_stock', 'available_units': 0}
    if stock.mode == 'availability':
        if policy.stock_policy == 'strict':
            return {**evidence, **_unknown('numeric_stock_required')}
        return {**evidence, 'status': 'in_stock', 'available_units': None,
                'reason': 'availability_flag_only'}
    units = max(0, stock.on_hand - stock.reserved - stock.pending_consumed)
    return {**evidence, 'status': 'in_stock' if units else 'out_of_stock',
            'available_units': units}


def inventory_knowledge(tenant_id):
    """Recompute on every knowledge request, including provider freshness.

    Variant stock overrides parent stock, matching commerce.services.reserve.
    The same parent pool can appear under several variants; it is not additive.
    No legacy MenuItem.quantity values are used as inventory evidence.
    """
    result = {'status': 'unknown', 'reason': 'inventory_not_configured', 'records': []}
    config = (Configuration.objects.filter(tenant_id=tenant_id, location__tenant_id=tenant_id)
              .select_related('location').first())
    if config is None:
        return result
    result.update(location_id=str(config.location_id), location_name=config.location.name)
    if not config.enabled and not config.local_checkout:
        return {**result, 'reason': 'commerce_disabled'}
    try:
        policy = Policy.model_validate(config.policy)
    except ValueError:
        return {**result, 'reason': 'invalid_inventory_policy'}
    result['stock_policy'] = policy.stock_policy
    if policy.stock_policy == 'untracked':
        return {**result, 'reason': 'inventory_untracked'}
    try:
        assert_menu_fresh(tenant_id)
    except ValueError:
        return {**result, 'reason': 'menu_unavailable_or_stale'}

    stocks = list(StockItem.objects.filter(location=config.location).filter(
        Q(item__tenant_id=tenant_id) | Q(variant__menu_item__tenant_id=tenant_id)
        | Q(addon__group__tenant_id=tenant_id)
    ).select_related('authority', 'addon__group').order_by('pk'))
    parents = {s.item_id: s for s in stocks if s.item_id}
    variants = {s.variant_id: s for s in stocks if s.variant_id}
    now = timezone.now()
    records = []
    rows = (MenuItemVariant.objects.filter(menu_item__tenant_id=tenant_id)
            .select_related('menu_item__category_fk').order_by('menu_item_id', 'pk'))
    for variant in rows:
        item = variant.menu_item
        record = {
            'kind': 'item_variant', 'item_id': str(item.pk), 'item_name': item.name,
            'item_aliases': (item.meta or {}).get('aliases', []),
            'variant_id': str(variant.pk), 'variant_name': variant.size,
            'variant_aliases': variant.aliases,
        }
        if (not item.is_available or not variant.is_available
                or (item.category_fk and not item.category_fk.is_active)):
            evidence = {'status': 'unavailable', 'available_units': None,
                        'reason': 'disabled_in_menu_not_a_physical_stock_count'}
        else:
            evidence = _stock_evidence(variants.get(variant.pk) or parents.get(item.pk), policy, now)
        records.append({**record, **evidence})
    for stock in stocks:
        if stock.addon_id:
            addon = stock.addon
            evidence = (_stock_evidence(stock, policy, now) if addon.is_available else
                        {'status': 'unavailable', 'available_units': None,
                         'reason': 'disabled_in_menu_not_a_physical_stock_count'})
            records.append({'kind': 'modifier', 'modifier_id': str(addon.pk),
                            'modifier_name': addon.name, 'modifier_aliases': addon.aliases,
                            'group_id': str(addon.group_id), 'group_name': addon.group.name,
                            **evidence})
    return {**result, 'status': 'checked', 'reason': None, 'records': records}
