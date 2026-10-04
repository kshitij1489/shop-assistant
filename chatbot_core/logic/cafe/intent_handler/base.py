"""Intent state and business contract used by the café conversation graph.

Routing, session persistence and business execution belong to the workflow.
Handoff requests and prompt restrictions are turn-local, not persisted commands.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from chatbot_core.capabilities import resolve_handler
from chatbot_core.logic.outcomes import TaskOutcome


def _intent_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Intent name must be a non-empty string")
    return name.strip().lower()


def get_intent(name: str) -> type[BaseIntent]:
    """Resolve only the requested handler, without importing unrelated services."""
    return resolve_handler(_intent_name(name))


def _mapping(value, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a mapping or null")
    return deepcopy(dict(value))


def _messages(value, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or any(not isinstance(message, str) for message in value):
        raise ValueError(f"{field} must be a list of strings or null")
    return list(value)


class BaseIntent(ABC):
    @property
    def is_complete(self):
        """Compatibility for handlers that still report a completion boolean."""
        return not self.outcome.resumable

    @is_complete.setter
    def is_complete(self, value):
        self.outcome = TaskOutcome.COMPLETED if value else TaskOutcome.NEEDS_CLARIFICATION

    def set_outcome(self, outcome, response):
        self.outcome = TaskOutcome(outcome)
        self.response = response
        self.follow_up_question[:] = [response] if self.outcome.resumable and response else []
        if self.outcome != TaskOutcome.NEEDS_CLARIFICATION:
            self.missing_fields = []
        return response

    # Identity is inherited from the current conversation, never from overrides.
    _HANDOFF_FIELDS = frozenset({
        "main_query", "sub_intent", "response", "is_complete", "ignored_count",
        "basket_item", "follow_up_question", "follow_up_reply", "delivery_address",
    })

    def __init__(self, *, main_query: str, sub_intent: str, tenant: int,
                 chat_id: str, query_id: int | str = 0, response: str = "",
                 is_complete: bool = False, ignored_count: int = 0,
                 basket_item: dict[str, Any] | None = None,
                 follow_up_question: list[str] | None = None,
                 follow_up_reply: list[str] | None = None) -> None:
        if not isinstance(is_complete, bool):
            raise ValueError("is_complete must be a boolean")
        if type(ignored_count) is not int or ignored_count < 0:
            raise ValueError("ignored_count must be a non-negative integer")
        self.main_query = main_query
        self.original_query = main_query
        self.rephrased_sentence: str | None = None
        self.response_language: str | None = None
        self.missing_fields = []
        self.sub_intent = sub_intent
        self.tenant = tenant
        self.chat_id = chat_id
        self.platform: str | None = None
        self.query_id = query_id
        self.response = response
        self.is_complete = is_complete
        self.ignored_count = ignored_count
        self.basket_item = _mapping(basket_item, "basket_item")
        self.follow_up_question = _messages(follow_up_question, "follow_up_question")
        self.follow_up_reply = _messages(follow_up_reply, "follow_up_reply")
        self.handoff_to: str | None = None
        self.handoff_overrides: dict[str, Any] = {}
        self.delivery_address: dict[str, Any] = {}
        # Executable actions are rebound each turn, never restored from history.
        self.resolved_action = None

    def answer_from_knowledge(self, knowledge, question):
        from chatbot_core.logic.cafe.prompts.answer_from_knowledge import answer_from_knowledge
        return answer_from_knowledge(
            knowledge, question, tenant_key=str(self.tenant), user_id=self.chat_id,
            platform=self.platform, sub_intent=self.sub_intent, main_intent=self.intent_type,
        )

    @abstractmethod
    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | str | None]:
        pass

    @abstractmethod
    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | str | None]:
        pass

    def get_followup_question(self) -> str:
        return self.follow_up_question[-1] if self.follow_up_question else ""

    def request_handoff(self, next_intent_name: str, **overrides) -> None:
        """Describe the next task; never execute it or change conversation scope."""
        name = _intent_name(next_intent_name)
        get_intent(name)  # Fail at the request boundary for unregistered targets.
        unknown = overrides.keys() - self._HANDOFF_FIELDS
        if unknown:
            raise ValueError(f"Unsupported handoff overrides: {', '.join(sorted(unknown))}")
        snapshot = deepcopy(overrides)
        self.handoff_to = name
        self.handoff_overrides = snapshot

    def build_handoff_intent(self) -> BaseIntent | None:
        """Build isolated state for the graph to queue, without running business work.

        Repeated builds are side-effect free. The graph consumes the request in
        the current turn; serialization deliberately excludes routing commands.
        """
        if self.handoff_to is None:
            return None
        name = _intent_name(self.handoff_to)
        target_class = get_intent(name)
        unknown = self.handoff_overrides.keys() - self._HANDOFF_FIELDS
        if unknown:
            raise ValueError(f"Unsupported handoff overrides: {', '.join(sorted(unknown))}")
        base_state = {
            "main_query": self.main_query,
            "sub_intent": name,
            "tenant": self.tenant,
            "chat_id": self.chat_id,
            "query_id": self.query_id,
            "response": "",
            "is_complete": False,
            "ignored_count": 0,
            "basket_item": self.basket_item,
            "follow_up_question": [],
            "follow_up_reply": [],
        }
        overrides = deepcopy(self.handoff_overrides)
        address = _mapping(overrides.pop("delivery_address", self.delivery_address), "delivery_address")
        base_state.update(overrides)
        target = target_class(**deepcopy(base_state))
        target.platform = self.platform
        target.response_language = self.response_language
        if target.main_query == self.main_query:
            target.original_query = self.original_query
            target.rephrased_sentence = self.rephrased_sentence
        target.delivery_address = address
        return target

    def to_dict(self) -> dict[str, Any]:
        """Return an owned session snapshot, excluding turn-local routing state."""
        return deepcopy({
            "main_query": self.main_query,
            "original_query": self.original_query,
            "rephrased_sentence": self.rephrased_sentence,
            "response_language": self.response_language,
            "missing_fields": self.missing_fields,
            "sub_intent": self.sub_intent,
            "tenant": self.tenant,
            "chat_id": self.chat_id,
            "platform": self.platform,
            "query_id": self.query_id,
            "response": self.response,
            "is_complete": self.is_complete,
            "outcome": self.outcome.value,
            "ignored_count": self.ignored_count,
            "basket_item": self.basket_item,
            "follow_up_question": self.follow_up_question,
            "follow_up_reply": self.follow_up_reply,
            "intent_type": getattr(self, "intent_type", None),
            "delivery_address": self.delivery_address,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BaseIntent:
        """Restore pending state without mutating or aliasing the stored payload."""
        if not isinstance(data, Mapping):
            raise ValueError("Intent state must be a mapping")
        intent_type = _intent_name(data.get("intent_type"))
        intent_class = get_intent(intent_type)
        # Validate collections before child constructors can coerce falsey input.
        basket = _mapping(data.get("basket_item"), "basket_item")
        questions = _messages(data.get("follow_up_question"), "follow_up_question")
        replies = _messages(data.get("follow_up_reply"), "follow_up_reply")
        address = data.get("delivery_address")
        # Older BaseIntent.from_dict defaulted this dictionary to an empty list.
        if isinstance(address, list) and not address:
            address = None
        address = _mapping(address, "delivery_address")
        obj = intent_class(
            main_query=data.get("main_query"), sub_intent=data.get("sub_intent"),
            tenant=data.get("tenant"), chat_id=data.get("chat_id"),
            query_id=data.get("query_id", 0), response=data.get("response", ""),
            is_complete=data.get("is_complete", False), ignored_count=data.get("ignored_count", 0),
            basket_item=basket, follow_up_question=questions, follow_up_reply=replies,
        )
        obj.intent_type = intent_type
        if 'outcome' in data:
            obj.outcome = TaskOutcome(data['outcome'])
        obj.platform = data.get("platform")
        obj.original_query = data.get("original_query", obj.main_query)
        obj.rephrased_sentence = data.get("rephrased_sentence")
        obj.response_language = data.get("response_language")
        obj.missing_fields = list(data.get("missing_fields", []))
        obj.delivery_address = address
        if intent_type == "order_enquiry":
            reference = data.get("order_reference")
            if reference is not None and not isinstance(reference, str):
                raise ValueError("order_reference must be a string or null")
            obj.order_reference = reference
        return obj
