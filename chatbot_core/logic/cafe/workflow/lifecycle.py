"""Durable checkout recovery shared by both conversation engines."""
from copy import deepcopy
import re
from uuid import uuid4


NEW_ORDER = re.compile(
    r"(?:please\s+)?(?:start\s+)?(?:a\s+)?new order(?:\s+please)?"
    r"(?:[.!]*|\s+(?:with|and)\s+\S.+|\s*[:,;]\s*\S.+)", re.I)
NEW_ORDER_ONLY = re.compile(
    r"(?:please\s+)?(?:start\s+)?(?:a\s+)?new order(?:\s+please)?[.!]*", re.I)


def pending_address_question(pending, incoming):
    if incoming.main_query.strip().lower() == 'cancel checkout':
        return None
    if incoming.intent_type == 'placing_order' and incoming.sub_intent in {'order_confirmation', 'order_payment'}:
        for intent in reversed(pending):
            if intent.intent_type == 'location_based' and intent.sub_intent in {
                    'add_delivery_address', 'update_delivery_address', 'confirm_delivery_address',
                    'choose_delivery_address'}:
                return 'Please finish or cancel the pending delivery address change before checkout.'
    return None


def reset_order_state(state):
    from chatbot_core.logic.cafe.basket import Basket
    state.update(basket=Basket(), delivery_address={},
                 checklist={'payment': False, 'order': False, 'location': False, 'order_id': None},
                 pending_queries=[], awaiting_followup_index=None, history=[])


def prepare_order_session(state, db_session, tenant, session, customer, query):
    """Restart an unfinished draft or roll over a terminal order on explicit request.

    Persist the new anchor first; its marker repairs a failed cache publication
    on the next turn without resurrecting the previous basket or checkout.
    """
    from django.db import transaction
    from orders.models import ChatSession, Order
    from chatbot_core.chat_session import complete_chat_session_by_order, create_new_chat_session
    reply = None
    restart = NEW_ORDER.fullmatch(query.strip())
    if restart and not getattr(db_session, 'order_id', None):
        with transaction.atomic():
            anchor = ChatSession.objects.select_for_update().get(pk=db_session.pk)
            if customer is None or anchor.customer_id != customer.pk:
                raise ValueError('Saved checkout does not match this customer')
            if anchor.order_id:
                return db_session, 'Your current order is still active. Please contact the café to change it.'
            anchor.state = {**(anchor.state or {}), 'checkout': {}, 'reset_order_state': str(uuid4())}
            anchor.save(update_fields=['state', 'last_interaction_at'])
            db_session = anchor
        reset_order_state(state)
        if NEW_ORDER_ONLY.fullmatch(query.strip()):
            reply = 'Started a new order. What would you like to add?'
    if restart and getattr(db_session, 'order_id', None):
        with transaction.atomic():
            anchor = ChatSession.objects.select_for_update().get(pk=db_session.pk)
            order = Order.objects.select_for_update().get(pk=anchor.order_id, tenant=tenant)
            if customer is None or order.customer_id != customer.pk:
                raise ValueError('Saved order does not match this customer')
            terminal = (order.order_status == Order.Status.CANCELLED or
                        order.order_status == Order.Status.DELIVERED and
                        order.payment_status == Order.PaymentStatus.PAID)
            if not terminal:
                return db_session, 'Your current order is still active. Please contact the café to change it.'
            complete_chat_session_by_order(order)
            db_session = create_new_chat_session(tenant, customer, session.platform, session.user_id,
                                                 defaults={'state': {'reset_order_state': True}})
        reset_order_state(state)
        # Let a request containing items continue through normal classification.
        if NEW_ORDER_ONLY.fullmatch(query.strip()):
            reply = 'Started a new order. What would you like to add?'
    if isinstance(getattr(db_session, 'state', None), dict):
        marker = f"{db_session.pk}:{db_session.state.get('reset_order_state', '')}"
        if (db_session.state.get('reset_order_state') and
                state['checklist'].get('_chat_session_id') != marker):
            reset_order_state(state)
        state['checklist']['_chat_session_id'] = marker
    return db_session, reply


def recover_checkout(state, db_session, tenant, session, customer):
    if isinstance(getattr(db_session, 'state', None), dict):
        draft = db_session.state.get('checkout')
        if 'checkout' in db_session.state and not draft:
            # Cancellation may have committed before the chat-cache save failed.
            state['checklist'].pop('checkout', None)
            state['pending_queries'] = [p for p in state['pending_queries'] if not p.basket_item.get('checkout')]
            state['awaiting_followup_index'] = len(state['pending_queries']) - 1 if state['pending_queries'] else None
        if isinstance(draft, dict) and draft:
            if customer is None or db_session.customer_id != customer.pk:
                raise ValueError('Saved checkout does not match this customer')
            # The database owns the checkout draft. Redis may have expired or a
            # previous turn may have failed after its database transaction.
            cached_draft = state['checklist'].get('checkout')
            recovering = not cached_draft
            state['checklist']['checkout'] = deepcopy(draft)
            if 'address_components' in draft:
                state['delivery_address'] = deepcopy(draft['address_components'])
            if db_session.order_id:
                state['checklist'].update(order=True, order_id=str(db_session.order_id))
            if draft.get('basket') and (
                    recovering and state['basket'].is_empty() or
                    cached_draft and cached_draft.get('basket') != draft['basket']):
                from chatbot_core.logic.cafe.basket import Basket
                state['basket'] = Basket.from_dict(deepcopy(draft['basket']))
            if not db_session.order_id and not any(p.basket_item.get('checkout') for p in state['pending_queries']):
                from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
                pending = PlacingOrderIntent(main_query='Resume checkout', sub_intent='order_confirmation',
                    tenant=tenant.id, chat_id=session.user_id, basket_item={'checkout': True},
                    follow_up_question=['Continue checkout or change your fulfillment mode.'])
                pending.platform = session.platform
                from chatbot_core.logic.outcomes import TaskOutcome
                pending.outcome = TaskOutcome(draft.get('outcome', TaskOutcome.NEEDS_CLARIFICATION))
                state['pending_queries'].append(pending)
                state['awaiting_followup_index'] = len(state['pending_queries']) - 1


def sync_checkout_basket(result, tenant, session, customer):
    if result['checklist'].get('checkout'):
        from django.db import transaction
        from orders.models import ChatSession
        with transaction.atomic():
            anchor = ChatSession.objects.select_for_update().filter(
                tenant=tenant, customer=customer, session_id=str(session.user_id), platform=session.platform,
                is_completed=False).order_by('-last_interaction_at', '-created_at', '-pk').first()
            if anchor and not anchor.order_id and (anchor.state or {}).get('checkout'):
                draft = anchor.state['checkout']
                if draft.get('basket') != result['basket'].to_dict():
                    draft['basket'] = deepcopy(result['basket'].to_dict())
                    draft.pop('quote', None)
                    anchor.save(update_fields=['state', 'last_interaction_at'])
