"""Versioned, deliberately bounded pricing and stock policy."""
from decimal import Decimal
from typing import Literal
from django.core.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError as SchemaError, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class TaxRule(Strict):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=100)
    rate: Decimal = Field(ge=0, le=100, decimal_places=4)
    inclusive: bool = False
    item_ids: list[str] = Field(default_factory=list)
    tax_fees: bool = False


class DiscountRule(Strict):
    code: str = Field(min_length=1, max_length=64)
    percent: Decimal = Field(default=Decimal('0'), ge=0, le=100, decimal_places=4)
    fixed_minor: int = Field(default=0, ge=0, strict=True)
    minimum_minor: int = Field(default=0, ge=0, strict=True)
    item_ids: list[str] = Field(default_factory=list)
    modes: list[Literal['delivery', 'pickup', 'dine_in']] = Field(default_factory=list)

    @model_validator(mode='after')
    def one_kind(self):
        if self.percent and self.fixed_minor:
            raise ValueError('Choose percentage or fixed discount, not both.')
        return self


# commerce/pricing.py rejects item and modifier quantities above this value and
# order totals above MAX_ORDER_MINOR. Tenant limits cannot exceed either ceiling.
MAX_ITEM_QUANTITY = 10_000
MAX_ORDER_MINOR = 9_999_999_999
# OrderItem.quantity is a PositiveIntegerField. Reject longer digit strings
# before converting them.
MAX_QUANTITY_DIGITS = 10


class OrderingLimits(Strict):
    """Operational caps. Absent on a policy means ordering is not enabled."""

    max_line_quantity: int = Field(ge=1, le=MAX_ITEM_QUANTITY, strict=True)
    max_item_quantity: int = Field(ge=1, le=MAX_ITEM_QUANTITY, strict=True)
    max_basket_units: int = Field(ge=1, le=MAX_ITEM_QUANTITY, strict=True)
    max_basket_lines: int = Field(ge=1, le=MAX_ITEM_QUANTITY, strict=True)
    max_subtotal_minor: int = Field(ge=1, le=MAX_ORDER_MINOR, strict=True)
    max_payable_minor: int = Field(ge=1, le=MAX_ORDER_MINOR, strict=True)

    @model_validator(mode='after')
    def consistent(self):
        if self.max_line_quantity > self.max_item_quantity:
            raise ValueError('The per-line quantity limit cannot exceed the per-item limit.')
        if self.max_item_quantity > self.max_basket_units:
            raise ValueError('The per-item quantity limit cannot exceed the basket unit limit.')
        if self.max_subtotal_minor > self.max_payable_minor:
            raise ValueError('The subtotal limit cannot exceed the payable limit.')
        return self


class Policy(Strict):
    schema_version: Literal[2] = 2
    currency: str = Field(default='INR', pattern=r'^[A-Z]{3}$')
    # The existing menu/order tables have two decimal places. Three-decimal
    # currencies need a catalog migration before they can be enabled.
    exponent: Literal[0, 2] = 2
    taxes: list[TaxRule] = Field(default_factory=list)
    discounts: list[DiscountRule] = Field(default_factory=list)
    packaging_minor: int = Field(default=0, ge=0, strict=True)
    minimum_minor: int = Field(default=0, ge=0, strict=True)
    stock_policy: Literal['strict', 'availability', 'untracked'] = 'strict'
    reservation_seconds: int = Field(default=900, ge=60, le=86400, strict=True)
    stock_max_age_seconds: int = Field(default=300, ge=1, le=86400, strict=True)
    # Explicitly absent limits in imported/existing policies keep ordering unavailable.
    ordering_limits: OrderingLimits | None = None

    @model_validator(mode='after')
    def unique_codes(self):
        currencies = {'INR': 2, 'EUR': 2, 'GBP': 2, 'USD': 2, 'CAD': 2, 'AUD': 2, 'CHF': 2, 'SEK': 2, 'NOK': 2, 'DKK': 2, 'PLN': 2, 'CZK': 2, 'JPY': 0}
        if currencies.get(self.currency) != self.exponent:
            raise ValueError('Unsupported currency or incorrect minor-unit exponent.')
        for rules in (self.taxes, self.discounts):
            if len({r.code for r in rules}) != len(rules):
                raise ValueError('Rule codes must be unique.')
        # Mixed inclusive/exclusive taxes per line require jurisdiction-specific
        # compounding; do not silently apply an incorrect tax base.
        if len({t.inclusive for t in self.taxes}) > 1:
            raise ValueError('Mixed inclusive and exclusive taxes are unsupported.')
        return self


def default_policy():
    """Conservative model default; starter limits are adopted only by onboarding."""
    return Policy().model_dump(mode='json')


# Frozen evaluation inputs, intentionally independent of editable product presets.
EVALUATION_ORDERING_LIMITS = {
    'max_line_quantity': 20,
    'max_item_quantity': 30,
    'max_basket_units': 60,
    'max_basket_lines': 20,
    'max_subtotal_minor': 500_000,
    'max_payable_minor': 600_000,
}


def evaluation_policy(**overrides):
    """Schema v2 policy using the evaluation caps. Not used as a model default."""
    data = Policy().model_dump(mode='json')
    data['ordering_limits'] = dict(EVALUATION_ORDERING_LIMITS)
    data.update(overrides)
    return Policy.model_validate(data).model_dump(mode='json')


def validate_policy(value):
    try:
        Policy.model_validate(value)
    except SchemaError as exc:
        raise ValidationError([error['msg'].removeprefix('Value error, ') for error in exc.errors(include_url=False)]) from exc
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
