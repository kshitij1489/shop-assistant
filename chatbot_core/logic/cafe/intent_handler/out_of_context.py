"""One-shot scope redirects; the workflow owns pending tasks and routing."""
import logging
from typing import Any

from chatbot_core.capabilities import CAPABILITIES
from .base import BaseIntent
from chatbot_core.logic.cafe.prompts.generate_response_from_knowledge import generate_response_from_knowledge

logger = logging.getLogger(__name__)


class OutOfContextIntent(BaseIntent):
    SUB_INTENT_NAMES = CAPABILITIES["out_of_context"].sub_intents
    FALLBACK_RESPONSE = "I can help with café information, menu items, and orders, but I can't help with that request."

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
        self.intent_type = "out_of_context"
        self.promp_restriction = False

    def _respond(self, api_key: str) -> tuple[str, int | None]:
        self.sub_intent = self.sub_intent.strip().lower() if isinstance(self.sub_intent, str) else ""
        response = self.FALLBACK_RESPONSE
        if self.sub_intent not in self.SUB_INTENT_NAMES:
            logger.warning("Unsupported out-of-context sub-intent: %s", self.sub_intent)
        elif isinstance(self.main_query, str) and self.main_query.strip():
            try:
                response = generate_response_from_knowledge(
                    api_key, self.sub_intent, self.main_query, main_intent=self.intent_type,
                    rephrased_sentence=self.rephrased_sentence, response_language=self.response_language,
                    promp_restriction=self.promp_restriction,
                    fallback_response=self.FALLBACK_RESPONSE,
                    response_profile="out_of_context",
                )
            except Exception:
                # A scope redirect must also work when knowledge/cache access
                # fails before the response helper reaches its provider guard.
                logger.exception("Out-of-context response generation failed")
            if not isinstance(response, str) or not response.strip():
                response = self.FALLBACK_RESPONSE
        self.response = response
        self.is_complete = True
        # Old sessions may have stored finished replies as pending questions.
        self.follow_up_question.clear()
        return self.response, None

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        return self._respond(api_key)

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        """Complete an older pending entry using the incoming classification."""
        self.rephrased_sentence = query_obj.rephrased_sentence
        self.response_language = query_obj.response_language
        self.main_query = query_obj.main_query
        self.sub_intent = query_obj.sub_intent
        self.follow_up_reply.append(self.main_query)
        self.promp_restriction = self.promp_restriction or getattr(query_obj, "promp_restriction", False)
        return self._respond(api_key)
