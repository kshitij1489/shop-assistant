"""Load/save the existing tenant-scoped session around one graph invocation."""
from copy import deepcopy
import logging
from evaluate.controls.telemetry import observed
from evaluate.controls.context import assert_scope

from chatbot_core.chat_session import create_new_chat_session, get_chat_ongoing_session
from chatbot_core.queues import enqueue_string
from chatbot_core.runtime_configuration import configuration_for_turn
from .graph import get_conversation_graph
from .order_context import remember_reply
from ..ordering_limits import public_summary
from .lifecycle import prepare_order_session, recover_checkout, sync_checkout_basket
from .state import ConversationContext, ConversationState

logger = logging.getLogger(__name__)


def load_state(session, tenant, query) -> ConversationState:
    # Copy even in-memory stores: failed graph turns must not mutate persisted objects.
    pending, awaiting = deepcopy(session.get_ongoing_queries())
    for intent in pending:
        if str(intent.tenant) != str(tenant.id) or str(intent.chat_id) != str(session.user_id) or intent.platform not in (None, session.platform):
            raise ValueError("Saved intent does not match session scope")
        intent.platform = session.platform
    return {
        "query": query, "counter": session.get_counter(),
        "basket": deepcopy(session.get_basket()),
        "delivery_address": deepcopy(session.get_delivery_address()),
        "checklist": deepcopy(session.get_checklist()), "history": deepcopy(session.get_history()),
        "pending_queries": pending, "awaiting_followup_index": awaiting,
        "intent_index": 0, "replies": [], "response_facts": [], "next_intent": None,
        "skip_followup_prompt": ("", False), "include_basket": True, "persist": True,
    }


def save_state(session, state):
    with session.turn():
        session.set_basket(state["basket"])
        session.set_delivery_address(state["delivery_address"])
        session.set_checklist(state["checklist"])
        session.set_ongoing_queries(state["pending_queries"], state["awaiting_followup_index"])
        session.set_history(state["history"])


@observed('workflow')
def run_conversation(tenant, session, query, customer=None):
    assert_scope(tenant.pk, customer.pk if customer else None)
    with session.turn(), configuration_for_turn(tenant.id):
        return _run_conversation(tenant, session, query, customer)


def _run_conversation(tenant, session, query, customer=None):
    if str(session.tenant_id) != str(tenant.id):
        raise ValueError("Session tenant does not match handler tenant")
    if customer is not None and str(customer.tenant_id) != str(tenant.id):
        raise ValueError("Customer tenant does not match handler tenant")
    state = load_state(session, tenant, query)
    db_session = get_chat_ongoing_session(session.user_id, tenant_id=tenant.id, platform=session.platform)
    if not db_session:
        db_session = create_new_chat_session(tenant=tenant, customer=customer, platform=session.platform,
                                session_id=session.user_id)
        state["basket"] = session.clear_basket()
        state["checklist"] = deepcopy(session.clear_checklist())
    db_session, reply = prepare_order_session(state, db_session, tenant, session, customer, query)
    recover_checkout(state, db_session, tenant, session, customer)
    if reply:
        save_state(session, state)
        return reply, public_summary(state['basket'], tenant)
    session.increment_counter()
    state["counter"] = session.get_counter()
    context = ConversationContext(tenant, customer, session.user_id, session.platform)
    enqueue_string(f'{state["counter"]}, User message: {query}')
    # There is one active node at a time. The high step limit accommodates long
    # split-message batches; intent_index advances monotonically through a finite list.
    result = get_conversation_graph().invoke(
        state, context=context, config={"recursion_limit": 10000, "max_concurrency": 1},
    )
    if result["persist"]:
        remember_reply(result, tenant)
        sync_checkout_basket(result, tenant, session, customer)
        save_state(session, result)
    logger.info("Returning café response for tenant=%s platform=%s", tenant.id, session.platform)
    enqueue_string(f'{state["counter"]}, Returning response, final_response: {result["response"]}')
    return result["response"], public_summary(result["basket"], tenant) if result["include_basket"] else None
