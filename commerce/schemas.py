"""Public adapter wire format; JSON Schema is published by the API."""
from datetime import datetime
from typing import Literal, Union, Annotated
from uuid import UUID
from pydantic import Field, model_validator
from .policy import Strict


class PaymentUpdate(Strict):
    type: Literal['payment.updated']
    payment_id: UUID
    external_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1, le=9223372036854775807, strict=True)
    currency: str = Field(pattern=r'^[A-Z]{3}$')
    status: Literal['pending', 'authorized', 'captured', 'failed', 'cancelled', 'refunded']
    captured_minor: int = Field(ge=0, le=9223372036854775807, strict=True)
    refunded_minor: int = Field(default=0, ge=0, le=9223372036854775807, strict=True)
    checkout_url: str = Field(default='', max_length=2000)


class StockUpdate(Strict):
    type: Literal['inventory.updated']
    stock_id: UUID
    sequence: int = Field(ge=1, le=9223372036854775807, strict=True)
    observed_at: datetime
    on_hand: int = Field(ge=0, le=2147483647, strict=True)
    available: bool = Field(strict=True)
    acknowledged_reservation_ids: list[UUID] = Field(default_factory=list, max_length=1000)


class OrderUpdate(Strict):
    type: Literal['order.updated']
    accepted_order_id: UUID
    external_id: str = Field(min_length=1, max_length=200)
    sequence: int = Field(ge=1, le=9223372036854775807, strict=True)
    status: Literal['accepted', 'preparing', 'dispatched', 'delivered', 'rejected', 'cancelled']


class Event(Strict):
    schema_version: Literal[1]
    event_id: str = Field(min_length=1, max_length=200)
    occurred_at: datetime
    data: Annotated[Union[PaymentUpdate, StockUpdate, OrderUpdate], Field(discriminator='type')]

    @model_validator(mode='after')
    def aware_times(self):
        if self.occurred_at.tzinfo is None or (isinstance(self.data, StockUpdate) and self.data.observed_at.tzinfo is None):
            raise ValueError('Timestamps require an explicit timezone.')
        return self


class Acknowledgement(Strict):
    lease_token: UUID
    outcome: Literal['succeeded', 'retry', 'unknown', 'failed']
    error_code: str = Field(default='', max_length=100, pattern=r'^[a-zA-Z0-9_.-]*$')


class MappingInput(Strict):
    kind: Literal['item', 'variant', 'modifier_group', 'modifier', 'category', 'tax', 'customer', 'order', 'payment', 'stock', 'location', 'order_line', 'fulfillment', 'pricing_tax', 'discount', 'fee']
    canonical_id: str = Field(min_length=1, max_length=100)
    external_id: str = Field(min_length=1, max_length=200)
    scope: str = Field(default='', max_length=200)
    revision: str = Field(default='', max_length=100)
    metadata: dict = Field(default_factory=dict)
