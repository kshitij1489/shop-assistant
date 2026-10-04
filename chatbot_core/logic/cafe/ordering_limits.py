"""Ordering limits shared by commerce and non-commerce basket paths."""
from decimal import Decimal

from django.apps import apps

from commerce.policy import MAX_ORDER_MINOR, Policy
from commerce.pricing import minor

from .catalog import selection_unit_total
from .ordering_errors import OrderingRejected

# One proposal is bounded before it is applied. The resulting basket is also
# checked against the tenant's max_basket_lines.
MAX_PROPOSAL_LINES = 50
MAX_MODIFIER_CHOICES = 100


def load_policy(*, tenant_id=None, api_key=None) -> Policy | None:
    """Return the tenant policy, including when commerce itself is disabled."""
    if not apps.is_installed('commerce'):
        return None
    from commerce.models import Configuration
    if tenant_id is None and api_key is not None:
        from chatbot_core.models import TenantInfo
        tenant_id = TenantInfo.objects.filter(api_key=api_key).values_list('pk', flat=True).first()
    if not tenant_id:
        return None
    row = Configuration.objects.filter(tenant_id=tenant_id).values_list('policy', flat=True).first()
    if row is None:
        return None
    try:
        return Policy.model_validate(row)
    except ValueError:
        return None


def format_minor(amount: int, exponent: int) -> str:
    """Format minor units for presentation. Calculation stays in integers."""
    if isinstance(exponent, bool) or not isinstance(exponent, int) or exponent < 0:
        raise ValueError('Invalid currency exponent.')
    sign = '-' if amount < 0 else ''
    digits = str(abs(amount))
    if exponent == 0:
        return sign + digits
    digits = digits.zfill(exponent + 1)
    return f'{sign}{digits[:-exponent]}.{digits[-exponent:]}'


def line_quantity(line: dict) -> int:
    quantity = line.get('quantity')
    if isinstance(quantity, bool) or type(quantity) is not int or quantity < 1:
        raise OrderingRejected('A basket quantity is not a positive whole number.')
    return quantity


def line_subtotal_minor(line: dict, exponent: int) -> int:
    """Base variant plus modifiers for the line, in minor units."""
    quantity = line_quantity(line)
    unit = minor(line.get('unit_price'), exponent)
    modifier_unit = 0
    for choice in line.get('modifiers') or []:
        count = choice.get('quantity')
        if isinstance(count, bool) or type(count) is not int or count < 1:
            raise OrderingRejected('A modifier quantity is not a positive whole number.')
        modifier_unit += minor(choice.get('unit_price'), exponent) * count
    return (unit + modifier_unit) * quantity


def basket_subtotal_minor(items: list, exponent: int) -> int:
    return sum(line_subtotal_minor(line, exponent) for line in items)


def _units(items: list) -> int:
    return sum(line_quantity(line) for line in items)


def _by_item(items: list) -> dict[str, int]:
    totals: dict[str, int] = {}
    for line in items:
        key = str(line.get('item_id'))
        totals[key] = totals.get(key, 0) + line_quantity(line)
    return totals


def limit_reason(items: list, policy: Policy) -> str | None:
    """Customer-facing reason the basket exceeds policy, or None when it does not."""
    limits = policy.ordering_limits
    if limits is None:
        return 'Ordering is unavailable until quantity and amount limits are configured.'
    if len(items) > limits.max_basket_lines:
        return f'A basket can contain at most {limits.max_basket_lines} lines.'
    units = 0
    by_item: dict[str, int] = {}
    for line in items:
        quantity = line_quantity(line)
        if quantity > limits.max_line_quantity:
            return f'You can order at most {limits.max_line_quantity} of one selection.'
        units += quantity
        key = str(line.get('item_id'))
        by_item[key] = by_item.get(key, 0) + quantity
        if by_item[key] > limits.max_item_quantity:
            return (
                f'You can order at most {limits.max_item_quantity} of one item, '
                'including every size and customization.'
            )
    if units > limits.max_basket_units:
        return f'A basket can contain at most {limits.max_basket_units} items.'
    subtotal = basket_subtotal_minor(items, policy.exponent)
    if subtotal > MAX_ORDER_MINOR or subtotal > limits.max_subtotal_minor:
        amount = format_minor(limits.max_subtotal_minor, policy.exponent)
        return f'That basket is above the subtotal limit of {amount} {policy.currency}.'
    return None


def _major_subtotal(items: list) -> Decimal:
    total = Decimal(0)
    for line in items:
        total += selection_unit_total(line) * line_quantity(line)
    return total


def is_nongrowth(previous: list, proposed: list) -> bool:
    """True when the proposal does not add a line or increase quantity or price."""
    if len(proposed) > len(previous):
        return False
    seen: dict = {}
    for line in previous:
        number = line.get('item_number')
        if number is None or number in seen:
            return False
        seen[number] = line
    try:
        for line in proposed:
            old = seen.get(line.get('item_number'))
            if old is None or line_quantity(line) > line_quantity(old):
                return False
        if _units(proposed) > _units(previous):
            return False
        before = _by_item(previous)
        if any(qty > before.get(key, 0) for key, qty in _by_item(proposed).items()):
            return False
        return _major_subtotal(proposed) <= _major_subtotal(previous)
    except (OrderingRejected, ValueError, ArithmeticError):
        return False


def enforce_change(previous: list, proposed: list, *, tenant_id=None, api_key=None) -> None:
    """Reject growth past the tenant limits. Reductions of an over-limit basket pass."""
    policy = load_policy(tenant_id=tenant_id, api_key=api_key)
    if policy is None or policy.ordering_limits is None:
        if is_nongrowth(previous, proposed):
            return
        raise OrderingRejected('Ordering is unavailable until quantity and amount limits are configured.')
    reason = limit_reason(proposed, policy)
    if reason is None or is_nongrowth(previous, proposed):
        return
    raise OrderingRejected(reason)


def assert_checkout(items: list, tenant, payable_minor: int) -> None:
    """Block checkout of an over-limit basket, including the post-fee payable total."""
    policy = load_policy(tenant_id=getattr(tenant, 'pk', tenant))
    if policy is None or policy.ordering_limits is None:
        raise OrderingRejected('Ordering is unavailable until quantity and amount limits are configured.')
    if limit_reason(items, policy):
        raise OrderingRejected('This basket is above the ordering limits. Reduce or remove items before checkout.')
    if isinstance(payable_minor, bool) or type(payable_minor) is not int or payable_minor < 0:
        raise OrderingRejected('The payable total is unavailable.')
    limit = policy.ordering_limits.max_payable_minor
    if payable_minor > MAX_ORDER_MINOR or payable_minor > limit:
        amount = format_minor(limit, policy.exponent)
        raise OrderingRejected(
            f'The order total is above the payable limit of {amount} {policy.currency}. '
            'Reduce the basket before checkout.'
        )


def public_summary(basket, tenant) -> list:
    """Customer basket payload. Empty baskets do not require a currency."""
    if basket.is_empty():
        return []
    policy = load_policy(tenant_id=getattr(tenant, 'pk', tenant))
    if policy is None:
        raise OrderingRejected('Ordering is unavailable until quantity and amount limits are configured.')
    return basket.summary(currency=policy.currency, exponent=policy.exponent)
