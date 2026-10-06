"""Validated model outputs. Nullable fields are required but may contain null."""

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class ModelOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FollowupDecision(ModelOutput):
    is_followup: bool


class AddressComponents(ModelOutput):
    street_address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    country: str | None


class TranslationResult(ModelOutput):
    source_lang: str
    confidence: float = Field(ge=0, le=1)
    translated: str


class SQLQuery(ModelOutput):
    sql: str | None
    params: list[str | int | float | bool | None]
    columns: list[str]


class SQLSafety(ModelOutput):
    is_safe: bool
    warnings: list[str]


class SQLProposal(ModelOutput):
    query: SQLQuery
    explanation: str
    safety: SQLSafety


class OrderModifier(ModelOutput):
    group_id: str
    option_id: str
    quantity: int


class EntityReference(ModelOutput):
    by: Literal["id", "name", "focus"]
    value: str | None = None


class OrderLineProposal(ModelOutput):
    action: Literal["add", "update", "remove", "replace"]
    item_id: str | None
    variant_id: str | None
    quantity: int | float | None = Field(description="Requested units, including invalid fractional values; never round or truncate. Package contents are not the number of packages.")
    modifiers: list[OrderModifier] | None = Field(description="null preserves existing modifiers on update; [] means standard")
    target_number: int | None
    reference: EntityReference | None = None
    unresolved: list[str]


class OrderProposal(ModelOutput):
    preserved_references: list[EntityReference] = Field(default_factory=list, description=(
        "Existing basket entries the customer explicitly wants kept unchanged. Identify these "
        "BEFORE proposing mutations. Include the retained item in remove X, keep Y; keep only Y; "
        "and remove everything except Y. Use catalog-language name references (or explicit row IDs). "
        "Do not include an item whose quantity or selection the customer requests changing."
    ))
    lines: list[OrderLineProposal] = Field(description=(
        "Only requested basket mutations, not all mentioned items. "
        "An existing item the customer keeps or says not to remove has NO line. "
        "Remove X and keep Y means one remove line for X only. "
        "Keep only Y means remove other existing entries, never Y. "
        "Choose each item's action independently; preserve negation and exceptions."
    ))
    unresolved: list[str]
    catalog_miss: bool


class ActionProposal(ModelOutput):
    kind: Literal["CHANGE_BASKET", "SHOW_CART", "SELECT_ADDRESS", "SET_FULFILLMENT",
                  "SET_PAYMENT_METHOD", "SET_CHECKOUT_FIELD", "CONTINUE_CHECKOUT",
                  "CLEAR_CHECKOUT_FIELD", "CONFIRM_ORDER", "RECOVER_PAYMENT", "CANCEL_PENDING_ACTION"]
    basket: OrderProposal | None = None
    reference: EntityReference | None = None
    field: Literal["name", "phone", "address", "postal_code", "table_id",
                   "scheduled_at", "discount_code"] | None = None
    value: str | None = None


class IntentClassification(ModelOutput):
    query: str
    # Historical decisions and deterministic commands predate English rewrites.
    rephrased_sentence: str | None = None
    intent: str
    sub_intent: str
    reply_to: str | None
    clarification: str | None
    action: ActionProposal | None = None


class ClassifiedMessages(ModelOutput):
    classifications: list[IntentClassification]
    declared_constraints: list[str] = Field(description="New explicit customer dietary/allergy requirements; informational questions alone do not declare a requirement")
    response_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(?:-[A-Za-z]{4})?$",
        description="Reply language and optional script, e.g. en, es, fr, ru, pt, hi-Latn for Roman Hindi. Null retains the conversation language for ambiguous short replies.")


class NormalizedIntentClassification(IntentClassification):
    rephrased_sentence: str = Field(min_length=1, description=(
        "Self-contained English meaning of this unit, with unambiguous spelling corrections. "
        "Preserve protected literals, quantities, negation, conditions and unresolved ambiguity. "
        "Resolve follow-ups only from the supplied context; never invent consent or facts."
    ))


class NormalizedClassifiedMessages(ClassifiedMessages):
    """Live normalization requires rewrites; historical decisions remain readable."""
    classifications: list[NormalizedIntentClassification]
