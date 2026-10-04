"""One-shot social replies. Conversation controls belong to the workflow."""
import logging

from chatbot_core.capabilities import CAPABILITIES
from .base import BaseIntent
from chatbot_core.logic.cafe.prompts.generate_response_from_knowledge import generate_response_from_knowledge

logger = logging.getLogger(__name__)


class GeneralIntent(BaseIntent):
    FALLBACKS = {
        "greeting": "Hello! How can I help you today?",
        "goodbye": "Goodbye! Have a lovely day.",
        "thanks": "You're welcome!",
        "small_talk": "I'm here to help with the café whenever you're ready.",
        "wait": "Take your time. Let me know when you're ready.",
    }
    SUB_INTENT_NAMES = CAPABILITIES["general"].sub_intents - {"cancel_and_abort"}
    UNKNOWN_RESPONSE = "I can help with café information, menu items, and orders."

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.intent_type = "general"
        self.promp_restriction = False

    def _respond(self, api_key, user_input, *, system_log_message=None):
        fallback = self.FALLBACKS.get(self.sub_intent)
        if self.sub_intent == "greeting" and self.promp_restriction:
            fallback = "Hello!"
        if fallback is None:
            logger.warning("Unsupported general sub-intent: %s", self.sub_intent)
            response = self.UNKNOWN_RESPONSE
        else:
            response = generate_response_from_knowledge(
                api_key, self.sub_intent, user_input, main_intent=self.intent_type,
                rephrased_sentence=self.rephrased_sentence, response_language=self.response_language,
                promp_restriction=self.promp_restriction,
                system_log_message=system_log_message,
                fallback_response=fallback,
            )
        self.response = response
        self.is_complete = True
        # General replies never create pending questions, including when loading
        # older sessions that stored a reply as a follow-up question.
        self.follow_up_question.clear()
        return self.response, None

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        return self._respond(api_key, self.main_query)

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        self.rephrased_sentence = query_obj.rephrased_sentence
        self.response_language = query_obj.response_language
        previous_response = self.response or self.get_followup_question()
        self.follow_up_reply.append(query_obj.main_query)
        self.sub_intent = query_obj.sub_intent
        self.promp_restriction = self.promp_restriction or getattr(query_obj, "promp_restriction", False)
        return self._respond(api_key, query_obj.main_query, system_log_message=previous_response)
