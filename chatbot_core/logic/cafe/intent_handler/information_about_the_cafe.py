"""One-shot café information answers; the workflow owns pending-task routing."""
import logging
from typing import Any

from chatbot_core.capabilities import CAPABILITIES
from chatbot_core.logic.cafe.knowledge_context import previous_knowledge_context
from .base import BaseIntent
from chatbot_core.logic.cafe.prompts.generate_response_from_knowledge import generate_response_from_knowledge

logger = logging.getLogger(__name__)


class InformationAboutCafeIntent(BaseIntent):
    """Answer tenant knowledge questions without creating pending workflow tasks."""

    SUB_INTENT_NAMES = CAPABILITIES["information_about_the_cafe"].sub_intents
    FALLBACK_RESPONSE = "Sorry, I don't have enough information to answer that café question right now."
    EMPTY_QUERY_RESPONSE = "Please tell me what you would like to know about the café."

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
        self.intent_type = "information_about_the_cafe"
        self.promp_restriction = False

    def _respond(self, api_key: str, user_input: str, *,
                 previous_user_message: str | None = None,
                 previous_response: str | None = None) -> tuple[str, int | None]:
        self.sub_intent = self.sub_intent.strip().lower() if isinstance(self.sub_intent, str) else ""
        if not self.sub_intent:
            logger.warning("Unsupported café information sub-intent: %s", self.sub_intent)
            response = self.FALLBACK_RESPONSE
        elif not isinstance(user_input, str) or not user_input.strip():
            response = self.EMPTY_QUERY_RESPONSE
        else:
            response = generate_response_from_knowledge(
                api_key, self.sub_intent, user_input, main_intent=self.intent_type,
                rephrased_sentence=self.rephrased_sentence, response_language=self.response_language,
                system_log_message=previous_response,
                previous_user_message=previous_user_message,
                promp_restriction=self.promp_restriction,
                fallback_response=self.FALLBACK_RESPONSE,
                response_profile="cafe_information",
            )
        self.response = response
        self.is_complete = True
        # Answers are history, not questions waiting for user input. Also clear
        # stale questions when processing intents restored from older sessions.
        self.follow_up_question.clear()
        return self.response, None

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        previous = previous_knowledge_context(self, history)
        return self._respond(
            api_key, self.main_query,
            previous_user_message=previous.get("previous_user_message"),
            previous_response=previous.get("system_log_message"),
        )

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        self.rephrased_sentence = query_obj.rephrased_sentence
        self.response_language = query_obj.response_language
        previous = previous_knowledge_context(query_obj, [{'query_obj': self.to_dict() | {
            'response': self.response or self.get_followup_question(),
        }}])
        self.follow_up_reply.append(query_obj.main_query)
        self.main_query = query_obj.main_query
        self.sub_intent = query_obj.sub_intent
        self.promp_restriction = self.promp_restriction or getattr(query_obj, "promp_restriction", False)
        return self._respond(
            api_key, self.main_query,
            previous_user_message=previous.get('previous_user_message'),
            previous_response=previous.get('system_log_message'),
        )
