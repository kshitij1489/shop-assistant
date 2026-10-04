"""Read-only order answers; LangGraph owns routing and session persistence."""
import logging
import re
import uuid
from typing import Any

from django.db import DatabaseError

from chatbot_core.capabilities import CAPABILITIES
from .base import BaseIntent
from chatbot_core.logic.cafe import db_utils
from chatbot_core.logic.cafe.order_support import SUPPORT_TOPICS, store_call_response

logger = logging.getLogger(__name__)


class OrderEnquiryIntent(BaseIntent):
    SUB_INTENT_NAMES = CAPABILITIES["order_enquiry"].sub_intents
    FALLBACK_RESPONSE = "I couldn’t check your order right now. Please try again shortly."
    UNKNOWN_RESPONSE = "I can help with order status, recent orders, or order support enquiries."
    EMPTY_QUERY_RESPONSE = "Please tell me what you would like to know about your order."
    CUSTOMER_REQUIRED_RESPONSE = "I don’t have a verified customer record here to check your orders. Please contact the café directly."
    REFERENCE_RESPONSE = "Please include one order ID from your receipt so I can check the correct order."
    CANCEL_TARGET_RESPONSE = ('Do you want to stop the current request or request cancellation of your placed order? '
                              'Reply “current request” or “order”.')

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
        self.intent_type = "order_enquiry"
        self.promp_restriction = False
        self.order_reference = None

    @staticmethod
    def _reference(query):
        """Extract receipt references without treating addresses/phones as order IDs."""
        references = re.findall(
            r"(?<![\w-])(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})(?![\w-])",
            query, re.I,
        )
        marker = r"\border\s*(?:id|number|no\.?|reference|ref\.?)\b\s*(?:is\s+)?[:#=]?\s*"
        references += re.findall(marker + r"([\w-]+)", query, re.I)
        references += re.findall(r"#([\w-]+)", query)
        references += re.findall(r"\border\s+([\w-]*\d[\w-]*)\b", query, re.I)
        # Numeric receipt lists can omit a second "order" or "#" marker.
        numeric_reference = r"(?=[\w-]*\d)[\w-]+"
        if re.search(r"\borders?\s+(?:(?:ids?|numbers?|references?)\s*[:=]?\s*)?"
                     + numeric_reference + r"\s*(?:,|and\b|or\b)\s*#?" + numeric_reference, query, re.I):
            raise ValueError("Multiple receipt references")
        if re.search(r"\border\s+(?:ids|numbers|references)\b", query, re.I):
            raise ValueError("Use a single receipt reference")
        if re.fullmatch(r"[\w-]*\d[\w-]*", query.strip()):
            references.append(query.strip())
        normalized = set()
        for reference in references:
            try:
                reference = str(uuid.UUID(reference))
            except ValueError:
                pass  # External receipt IDs need not be UUIDs.
            normalized.add(reference)
        if len(normalized) > 1 or (not normalized and re.search(marker, query, re.I)):
            raise ValueError("A single receipt reference is required")
        return next(iter(normalized), None)

    def _previous_reference(self, history):
        last = history[-1] if history else None
        previous = last.get("query_obj") if isinstance(last, dict) else None
        if not isinstance(previous, dict) or (
            previous.get("intent_type") != self.intent_type
            or str(previous.get("tenant")) != str(self.tenant)
            or previous.get("chat_id") != self.chat_id
            or previous.get("platform") != self.platform
        ):
            return None
        reference = previous.get("order_reference")
        if isinstance(reference, str) and reference:
            return reference
        # Older saved intents only have the original question.
        query = previous.get("main_query")
        return self._reference(query) if isinstance(query, str) else None

    def _answer(self, customer, history, *, require_reference=False):
        if self.sub_intent not in self.SUB_INTENT_NAMES:
            logger.warning("Unsupported order enquiry sub-intent: %s", self.sub_intent)
            return self.UNKNOWN_RESPONSE
        if not isinstance(self.main_query, str) or not self.main_query.strip():
            return self.EMPTY_QUERY_RESPONSE
        if self.sub_intent in SUPPORT_TOPICS:
            # Remember an optional conversational reference without looking up
            # the order or requiring the customer to supply/clarify an ID.
            try:
                self.order_reference = self._reference(self.main_query)
                if self.order_reference is None and not re.search(
                        r'\b(latest|last|newest|most recent)\b', self.main_query, re.I):
                    self.order_reference = self._previous_reference(history)
            except ValueError:
                self.order_reference = None
            return store_call_response(tenant_id=self.tenant)
        if (customer is None or not getattr(customer, "pk", None)
                or str(getattr(customer, "tenant_id", None)) != str(self.tenant)):
            return self.CUSTOMER_REQUIRED_RESPONSE
        if self.sub_intent == "get_order_history":
            return db_utils.get_order_history(customer)
        if self.sub_intent == "general_order_enquiry":
            return db_utils.general_order_enquiry(customer.tenant, customer, self.main_query)

        try:
            reference = self._reference(self.main_query)
            if require_reference and reference is None and not re.search(
                    r'\b(latest|last|newest|most recent)\b', self.main_query, re.I):
                self.basket_item['awaiting_order_reference'] = True
                return self.REFERENCE_RESPONSE
            # Elliptical follow-ups use only the immediately preceding, scoped
            # enquiry. A request for the latest order explicitly resets selection.
            if reference is None and not re.search(r"\b(latest|last|newest|most recent)\b", self.main_query, re.I):
                if re.search(r"\b(it|its|that|this|same)\b|^\s*and\b", self.main_query, re.I):
                    reference = self._previous_reference(history)
        except ValueError:
            self.basket_item['awaiting_order_reference'] = True
            return self.REFERENCE_RESPONSE
        self.order_reference = reference
        if self.sub_intent == "order_status_tracking":
            response = db_utils.order_status(customer, order_id=reference)
            if response:
                return response
            message = ("I couldn’t find that order in the records available to this chat. Check the order ID on your receipt. "
                       if reference is not None else "I couldn’t find any orders in the records available to this chat. ")
            if reference is not None:
                self.basket_item['awaiting_order_reference'] = True
                return message + self.REFERENCE_RESPONSE
            return message + db_utils.support_contacts_line(customer.tenant)
        return self.UNKNOWN_RESPONSE

    def _respond(self, customer, history) -> tuple[str, int | None]:
        if self.basket_item.get('clarify_cancel_target'):
            self.response = self.CANCEL_TARGET_RESPONSE
            self.is_complete = False
            self.follow_up_question[:] = [self.response]
            return self.response, None
        self.sub_intent = self.sub_intent.strip().lower() if isinstance(self.sub_intent, str) else ""
        self.order_reference = None
        require_reference = self.basket_item.pop('awaiting_order_reference', False)
        try:
            response = self._answer(customer, history, require_reference=require_reference)
        except DatabaseError:
            logger.exception("Order enquiry lookup failed for query_id=%s", self.query_id)
            response = self.FALLBACK_RESPONSE
        self.response = response if isinstance(response, str) and response.strip() else self.FALLBACK_RESPONSE
        self.is_complete = not self.basket_item.get('awaiting_order_reference', False)
        self.follow_up_question[:] = [] if self.is_complete else [self.response]
        self.handoff_to = None
        self.handoff_overrides = {}
        return self.response, None

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        return self._respond(customer, history)

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        if query_obj.basket_item.get('clarify_cancel_target'):
            return self._respond(customer, history)
        previous = self.to_dict()
        self.basket_item.pop('clarify_cancel_target', None)
        self.basket_item.pop('cancel_task_id', None)
        self.main_query = query_obj.main_query
        if query_obj.intent_type == self.intent_type:
            self.sub_intent = query_obj.sub_intent
        self.follow_up_reply.append(query_obj.main_query)
        return self._respond(customer, [{"query_obj": previous}])

    def to_dict(self):
        # Completed answers live in graph history, not a pending business task.
        return {**super().to_dict(), "order_reference": self.order_reference}
