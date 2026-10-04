from datetime import timedelta
from django.utils import timezone
from orders.models import CheckoutSettings, MenuItem
from orders.checkout_config import CheckoutPolicy
from .policy import Policy
from .credentials import adapter_secret
from .models import Configuration, Connection, StockItem


def readiness_issues(tenant, *, configuration=None, policy=None):
    """Local prerequisites only; a configured adapter is not proof of live validation."""
    issues = []
    from .menu_sync import assert_menu_fresh
    try:
        assert_menu_fresh(tenant.pk)
    except ValueError as exc:
        issues.append(str(exc))
    checkout = CheckoutSettings.objects.filter(tenant=tenant).first()
    config = configuration or Configuration.objects.filter(tenant=tenant).first()
    if not checkout:
        issues.append('Save checkout settings to replace the legacy checkout for this tenant.')
    if not config:
        issues.append('Save commerce settings before configuring connections and stock.')
        return issues
    policy = Policy.model_validate(policy or config.policy).model_dump(mode='json')
    connections = Connection.objects.filter(location=config.location, active=True)
    pos = connections.filter(role='pos').first()
    if not pos or not {'order.submit', 'order.reconcile'} <= set(pos.capabilities) or not adapter_secret(pos):
        issues.append('Configure and activate a POS adapter with order submission and reconciliation.')
    checkout_policy = CheckoutPolicy.model_validate(checkout.configuration).model_dump(mode='json') if checkout else None
    online = checkout_policy and any('online' in mode['payment_methods'] for mode in checkout_policy['modes'].values())
    if online:
        gateway = connections.filter(role='payment').first()
        if checkout_policy['online_provider'] != 'adapter':
            issues.append('Choose the commerce adapter as the online payment provider in checkout settings.')
        if not gateway or not {'payment.create', 'payment.reconcile'} <= set(gateway.capabilities) or not adapter_secret(gateway):
            issues.append('Configure and activate a payment adapter with payment creation and reconciliation.')
    if policy['stock_policy'] != 'untracked':
        stocks = list(StockItem.objects.filter(location=config.location).select_related('authority'))
        item_ids = {s.item_id for s in stocks if s.item_id}
        variant_ids = {s.variant_id for s in stocks if s.variant_id}
        items = MenuItem.objects.filter(tenant=tenant, is_available=True).prefetch_related('variants', 'category_fk')
        for item in items:
            if item.category_fk and not item.category_fk.is_active:
                continue
            variants = [v for v in item.variants.all() if v.is_available]
            if item.pk not in item_ids and (not variants or any(v.pk not in variant_ids for v in variants)):
                issues.append(f'Configure stock for {item.name} or each of its variants.')
        cutoff = timezone.now() - timedelta(seconds=policy['stock_max_age_seconds'])
        if any(s.authority_id and (not s.observed_at or s.observed_at < cutoff) for s in stocks):
            issues.append('Refresh provider stock through its adapter before enabling commerce.')
        if any(s.authority_id and (not s.authority.active or 'inventory.update' not in s.authority.capabilities) for s in stocks):
            issues.append('Activate the inventory adapter responsible for provider stock.')
        if policy['stock_policy'] == 'strict' and any(s.mode != 'quantity' for s in stocks):
            issues.append('Strict stock requires numeric quantities for all stock records.')
    return issues
