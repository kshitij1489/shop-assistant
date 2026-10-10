"""Validated checkout policy shared by the dashboard and conversation engine."""
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError as SchemaError, model_validator

MODES = ('delivery', 'pickup', 'dine_in')
FIELDS = {
    'delivery': ('name', 'phone', 'address', 'postal_code'),
    'pickup': ('name', 'phone', 'scheduled_at'),
    'dine_in': ('name', 'phone', 'table_id'),
}


def online_provider_ready(provider, tenant=None):
    if provider != 'adapter' or tenant is None:
        return False
    from django.apps import apps
    if not apps.is_installed('commerce'):
        return False
    from commerce.models import Configuration, Connection
    from commerce.credentials import adapter_secret
    config = Configuration.objects.filter(tenant=tenant, enabled=True).first()
    if not config:
        return False
    gateway = Connection.objects.filter(location=config.location, role='payment', active=True).first()
    return bool(gateway and {'payment.create', 'payment.reconcile'} <= set(gateway.capabilities)
                and adapter_secret(gateway))


def validate_online_readiness(value, tenant):
    policy = CheckoutPolicy.model_validate(value)
    if any('online' in mode.payment_methods for mode in policy.modes.values()):
        if not online_provider_ready(policy.online_provider, tenant):
            raise ValidationError('Online checkout requires enabled commerce and an active payment adapter with credentials, creation and reconciliation capabilities.')


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ModePolicy(StrictModel):
    required_fields: list[str] = Field(default_factory=list)
    payment_methods: list[Literal['cash', 'online']] = Field(default_factory=lambda: ['cash'], min_length=1)
    preparation_minutes: int = Field(default=20, ge=0, le=1440, strict=True)
    scheduling_enabled: bool = Field(default=False, strict=True)
    max_advance_days: int = Field(default=7, ge=1, le=365, strict=True)
    minimum_order: Decimal = Field(default=Decimal('0'), ge=0, max_digits=10, decimal_places=2)
    fee: Decimal = Field(default=Decimal('0'), ge=0, max_digits=10, decimal_places=2)


class CheckoutPolicy(StrictModel):
    timezone: str = 'Asia/Kolkata'
    modes: dict[str, ModePolicy] = Field(min_length=1)
    # Missing day is closed; empty mapping explicitly means always open.
    opening_hours: dict[str, list[list[str]]] = Field(default_factory=dict)
    delivery_postal_codes: list[str] = Field(default_factory=list)
    online_provider: Literal['', 'adapter'] = ''

    @model_validator(mode='after')
    def supported_combinations(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('Choose a valid IANA timezone.')
        for mode, policy in self.modes.items():
            if mode not in MODES:
                raise ValueError(f'Unsupported fulfillment mode: {mode}.')
            if set(policy.required_fields) - set(FIELDS[mode]):
                raise ValueError(f'Unsupported required fields for {mode}.')
            if mode == 'delivery' and 'address' not in policy.required_fields:
                raise ValueError('Delivery requires an address.')
            if 'scheduled_at' in policy.required_fields and not policy.scheduling_enabled:
                raise ValueError('A required pickup time needs scheduling enabled.')
            if 'online' in policy.payment_methods and self.online_provider != 'adapter':
                raise ValueError('Online payment requires an external payment adapter.')
        if self.delivery_postal_codes and 'delivery' not in self.modes:
            raise ValueError('Delivery coverage requires delivery to be enabled.')
        if any(not code.strip() or len(code) > 20 for code in self.delivery_postal_codes):
            raise ValueError('Enter nonempty delivery postal codes (up to 20 characters).')
        self.delivery_postal_codes = sorted(set(code.strip().upper() for code in self.delivery_postal_codes))
        from datetime import time
        for day, intervals in self.opening_hours.items():
            if day not in [str(i) for i in range(7)]:
                raise ValueError('Opening days use 0 (Monday) through 6 (Sunday).')
            previous_end = ''
            for interval in intervals:
                if len(interval) != 2:
                    raise ValueError('Each opening interval needs a start and end time.')
                start, end = interval
                try:
                    for index, value in enumerate(interval):
                        if index == 1 and value == '24:00':
                            continue
                        if len(value) != 5 or time.fromisoformat(value).strftime('%H:%M') != value:
                            raise ValueError()
                except ValueError:
                    raise ValueError('Opening times must use HH:MM.')
                if start >= end or start < previous_end:
                    raise ValueError('Opening intervals must be sorted, nonoverlapping and within one day; split overnight hours across days.')
                previous_end = end
        return self


def default_checkout_config():
    from .settings_defaults import ordering_defaults
    return ordering_defaults()['checkout']


def validate_checkout_config(value):
    try:
        CheckoutPolicy.model_validate(value)
    except SchemaError as exc:
        raise ValidationError([error['msg'].removeprefix('Value error, ') for error in exc.errors(include_url=False)]) from exc
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
