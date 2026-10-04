"""Outgoing v1 command schemas. Response readers must tolerate added fields."""
from datetime import datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Response(BaseModel):
    model_config = ConfigDict(extra='allow')


Money = Annotated[int, Field(ge=0, strict=True)]
Identifier = Annotated[str, Field(min_length=1)]
Currency = Annotated[str, Field(pattern=r'^[A-Z]{3}$')]


class Customer(Response):
    id: UUID | None
    name: str
    phone: str


class Tax(Response):
    code: str
    name: str
    rate: str
    inclusive: bool
    amount_minor: Money


class PricedComponent(Response):
    subtotal_minor: Money
    discount_minor: Money
    tax_minor: Money
    net_minor: Money
    total_minor: Money
    taxes: list[Tax]


class Modifier(Response):
    option_id: Identifier
    quantity: Annotated[int, Field(ge=1, strict=True)]
    unit_price: str
    unit_minor: Money
    subtotal_minor: Money


class Line(PricedComponent):
    line_id: Identifier
    item_id: Identifier
    item_variant_id: Identifier
    name: str
    quantity: Annotated[int, Field(ge=1, strict=True)]
    unit_price: str
    unit_minor: Money
    modifiers: list[Modifier]


class Fee(PricedComponent):
    code: str


class Pricing(Response):
    schema_version: Literal[1]
    currency: Currency
    exponent: Literal[0, 2]
    rounding: Literal['HALF_UP_PER_LINE_LARGEST_REMAINDER']
    policy: dict
    lines: list[Line]
    fees: list[Fee]
    subtotal_minor: Money
    discount_code: str
    discount_minor: Money
    tax_minor: Money
    total_minor: Money
    location_id: UUID


class Fulfillment(Response):
    fulfillment_id: Identifier


class Snapshot(Response):
    schema_version: Literal[1]
    order_id: UUID
    tenant_id: Identifier
    location_id: UUID
    source: str
    customer: Customer
    fulfillment: Fulfillment
    instructions: str
    pricing: Pricing


class PaymentCreateData(Response):
    payment_id: UUID
    order_id: UUID
    currency: Currency
    exponent: Literal[0, 2]
    amount_minor: Money
    customer: Customer
    # Older v1 deliveries did not include this field.
    expires_at: datetime | None = None


class PaymentReconcileData(Response):
    payment_id: UUID
    external_id: str | None


class PaymentRefundData(PaymentReconcileData):
    external_id: Identifier
    currency: Currency
    exponent: Literal[0, 2]
    target_refunded_minor: Money


class PaymentReference(Response):
    payment_id: UUID
    external_id: str
    provider: str
    currency: Currency
    captured_minor: Money
    refunded_minor: Money


class ReservationReference(Response):
    id: UUID
    stock_id: UUID
    quantity: Annotated[int, Field(ge=1, strict=True)]


class OrderSubmitData(Response):
    accepted_order_id: UUID
    order_id: UUID
    snapshot_hash: Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
    snapshot: Snapshot
    payment_method: Literal['cash', 'online']
    payments: list[PaymentReference]
    reservations: list[ReservationReference]


class OrderReconcileData(Response):
    accepted_order_id: UUID
    order_id: UUID
    original_command_id: UUID


class CommandEnvelope(Response):
    schema_version: Literal[1]
    command_id: UUID
    idempotency_key: UUID
    lease_token: UUID
    lease_until: datetime
    attempt: Annotated[int, Field(ge=1, strict=True)]


class PaymentCreate(CommandEnvelope):
    type: Literal['payment.create']
    data: PaymentCreateData


class PaymentReconcile(CommandEnvelope):
    type: Literal['payment.reconcile']
    data: PaymentReconcileData


class PaymentRefund(CommandEnvelope):
    type: Literal['payment.refund']
    data: PaymentRefundData


class OrderSubmit(CommandEnvelope):
    type: Literal['order.submit']
    data: OrderSubmitData


class OrderReconcile(CommandEnvelope):
    type: Literal['order.reconcile']
    data: OrderReconcileData


Command = Annotated[Union[PaymentCreate, PaymentReconcile, PaymentRefund, OrderSubmit, OrderReconcile], Field(discriminator='type')]
command_schema = TypeAdapter(Command)


class ClaimResponse(Response):
    schema_version: Literal[1]
    commands: list[Command] = Field(max_length=20)
