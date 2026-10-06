"""Per-invocation state; tenant/customer objects belong to runtime context."""
from dataclasses import dataclass
from typing import Literal, TypedDict

from chatbot_core.models import TenantInfo
from orders.models import Customer
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.base import BaseIntent
from chatbot_core.llm.schemas import ActionProposal


@dataclass(frozen=True)
class ConversationContext:
    tenant: TenantInfo
    customer: Customer | None
    user_id: str
    platform: str


class ConversationState(TypedDict, total=False):
    query: str
    counter: int
    basket: Basket
    delivery_address: dict
    checklist: dict
    history: list[dict]
    pending_queries: list[BaseIntent]
    awaiting_followup_index: int | None
    previous_followup_index: int | None
    previous_followup: BaseIntent | None
    paused_intent: BaseIntent | None
    followup_question: str
    followup_main_query: str
    classifications: list[tuple[str, str, str, str | None, str | None]]
    actions: list[ActionProposal | None]
    rephrased_sentences: list[str | None]
    saved_addresses: list[dict]
    offered_quote_fingerprint: str | None
    intent_index: int
    active_intent: BaseIntent | None
    matched_followup_index: int | None
    resolution: Literal["cancel", "followup", "new", "pause", "unavailable", "clarify"]
    current_reply: str
    replies: list[str]
    current_response_context: dict
    response_facts: list[dict]
    basket_before_intent: list[dict]
    next_intent: BaseIntent | None
    skip_followup_prompt: tuple[object, bool]
    response: str | list[str]
    include_basket: bool
    persist: bool

    question_intent: BaseIntent | None
    delivered_question: str
