"""Bounded clarification state; the workflow owns routing to other intents."""
import logging
from typing import Any

from chatbot_core.capabilities import CAPABILITIES
from .base import BaseIntent
from chatbot_core.logic.cafe.prompts.clarify_user_message import clarify_user_message

logger = logging.getLogger(__name__)


class InsufficientInformationIntent(BaseIntent):
    SUB_INTENT_NAMES = CAPABILITIES["insufficient_information"].sub_intents
    MAX_CLARIFICATION_QUESTIONS = 2
    FALLBACK_RESPONSE = "Sorry, I couldn't understand that. Could you rephrase your café question or order request?"
    RESTRICTED_RESPONSE = "Sorry, I couldn't understand that."
    EXHAUSTED_RESPONSE = "I'm still unable to understand this request. You can start a new request about café information, menu items, or an order."

    def __init__(self, *, main_query: str, sub_intent: str, tenant: int,
                 chat_id: str, query_id: int = 0, response: str = "",
                 is_complete: bool = False, ignored_count: int = 0,
                 basket_item: dict[str, Any] | None = None,
                 follow_up_question: list[str] | None = None,
                 follow_up_reply: list[str] | None = None) -> None:
        super().__init__(
            main_query=main_query, sub_intent=sub_intent, tenant=tenant,
            chat_id=chat_id, query_id=query_id, response=response,
            is_complete=is_complete, ignored_count=ignored_count,
            basket_item=basket_item or {}, follow_up_question=follow_up_question or [],
            follow_up_reply=follow_up_reply or [],
        )
        self.intent_type = "insufficient_information"
        self.promp_restriction = False

    def _respond(self, *, previous_query=None, previous_response=None) -> tuple[str, int | None]:
        if self.is_complete:
            self.follow_up_question.clear()
            self.response = self.response or self.EXHAUSTED_RESPONSE
            return self.response, None
        self.sub_intent = self.sub_intent.strip().lower() if isinstance(self.sub_intent, str) else ""
        if self.sub_intent not in self.SUB_INTENT_NAMES:
            logger.warning("Unsupported clarification sub-intent: %s", self.sub_intent)
            self.response = self.EXHAUSTED_RESPONSE
            self.is_complete = True
        elif self.promp_restriction:
            # The graph will supply the existing task's question.
            self.response = self.RESTRICTED_RESPONSE
            self.is_complete = True
        elif len(self.follow_up_question) >= self.MAX_CLARIFICATION_QUESTIONS:
            self.response = self.EXHAUSTED_RESPONSE
            self.is_complete = True
        else:
            self.response = clarify_user_message(
                self.main_query, previous_query=previous_query,
                previous_response=previous_response, fallback=self.FALLBACK_RESPONSE,
            )
            self.follow_up_question.append(self.response)
        if self.is_complete:
            self.follow_up_question.clear()
        return self.response, None

    def insufficient_information(self, api_key):
        """Compatibility entry point; clarification requires no tenant knowledge."""
        return self._respond()[0]

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        return self._respond()

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        if self.is_complete:
            return self._respond()
        previous_query = self.main_query
        previous_response = self.response or self.get_followup_question()
        self.main_query = query_obj.main_query
        self.sub_intent = query_obj.sub_intent
        self.follow_up_reply.append(query_obj.main_query)
        self.promp_restriction = self.promp_restriction or getattr(query_obj, "promp_restriction", False)
        return self._respond(previous_query=previous_query, previous_response=previous_response)
