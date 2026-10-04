"""Menu knowledge answers; the conversation graph owns routing and pending work."""
import logging
from typing import Any

from chatbot_core.capabilities import CAPABILITIES
from chatbot_core.logic.cafe.knowledge_context import previous_knowledge_context
from .base import BaseIntent
from chatbot_core.logic.cafe.prompts.generate_response_from_knowledge import generate_response_from_knowledge

logger = logging.getLogger(__name__)


class MenuItemsIntent(BaseIntent):
    SUB_INTENT_NAMES = CAPABILITIES["menu_items"].sub_intents
    FALLBACK_RESPONSE = "Sorry, I don't have enough information to answer that menu question right now."
    EMPTY_QUERY_RESPONSE = "Please tell me what you would like to know about the menu."

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
        self.intent_type = "menu_items"
        self.promp_restriction = False

    def _respond(self, api_key: str, *, previous_user_message: str | None = None,
                 previous_response: str | None = None) -> tuple[str, int | None]:
        self.sub_intent = self.sub_intent.strip().lower() if isinstance(self.sub_intent, str) else ""
        if self.sub_intent not in self.SUB_INTENT_NAMES:
            logger.warning("Unsupported menu sub-intent: %s", self.sub_intent)
            response = self.FALLBACK_RESPONSE
        elif not isinstance(self.main_query, str) or not self.main_query.strip():
            response = self.EMPTY_QUERY_RESPONSE
        else:
            response = generate_response_from_knowledge(
                api_key, self.sub_intent, self.main_query, main_intent=self.intent_type,
                rephrased_sentence=self.rephrased_sentence, response_language=self.response_language,
                system_log_message=previous_response,
                previous_user_message=previous_user_message,
                promp_restriction=self.promp_restriction,
                fallback_response=self.FALLBACK_RESPONSE,
                response_profile="menu_items",
            )
        self.response = response
        self.is_complete = True
        # Response punctuation must not create a pending task. Clear questions
        # from older serialized sessions as well as from reused instances.
        self.follow_up_question.clear()
        return self.response, None

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        previous = previous_knowledge_context(self, history)
        return self._respond(
            api_key, previous_user_message=previous.get("previous_user_message"),
            previous_response=previous.get("system_log_message"),
        )

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        self.rephrased_sentence = query_obj.rephrased_sentence
        self.response_language = query_obj.response_language
        # The graph can still route replies to pending menu tasks saved by an
        # older deployment. Resolve them using the newly classified category.
        previous = previous_knowledge_context(query_obj, [{'query_obj': self.to_dict() | {
            'response': self.response or self.get_followup_question(),
        }}])
        self.main_query = query_obj.main_query
        self.sub_intent = query_obj.sub_intent
        self.follow_up_reply.append(self.main_query)
        self.promp_restriction = self.promp_restriction or getattr(query_obj, "promp_restriction", False)
        return self._respond(
            api_key, previous_user_message=previous.get('previous_user_message'),
            previous_response=previous.get('system_log_message'),
        )
