"""Build typed actions for tests that used to pass prose into basket execution."""
import re

from chatbot_core.llm.schemas import ActionProposal
from chatbot_core.logic.action_resolver import NeedsClarification, ResolvedAction, resolve_action
from chatbot_core.logic.cafe.checkout import CONFIRM, mode_request
from chatbot_core.logic.cafe.order_changes import apply_proposal


CHECKOUT_TOPICS = {'order_confirmation', 'order_payment', 'order_channels_and_modes', 'order_scheduling'}
FIELD_NAMES = {'postal code': 'postal_code', 'table': 'table_id', 'table id': 'table_id'}
SCHEDULED = re.compile(
    r'(?:schedule(?:\s+(?:for|at))?\s+|pickup at\s+)?'
    r'(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?)', re.I)
LABELLED_FIELD = re.compile(r'(name|phone|address|postal code|table(?: id)?):\s*(.+)', re.I)


def proposal_body(value):
    """Accept a raw proposal dict or the {'proposal': ...} parser envelope."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump()
    if isinstance(value, dict) and 'lines' not in value and isinstance(value.get('proposal'), dict):
        return value['proposal']
    return value


def change_action(value):
    """Turn a scripted basket proposal into a CHANGE_BASKET action.

    An explicit reference is kept. Otherwise a target number is the customer's
    entry id; the resolver ignores a guessed target_number on the line.
    """
    body = proposal_body(value)
    lines = []
    for line in body.get('lines', []):
        row = dict(line)
        if row.get('reference') is None and row.get('action') != 'add' and row.get('target_number') is not None:
            row['reference'] = {'by': 'id', 'value': str(row['target_number'])}
        row.setdefault('unresolved', [])
        lines.append(row)
    return ActionProposal(kind='CHANGE_BASKET', basket={
        'lines': lines, 'unresolved': list(body.get('unresolved') or []),
        'catalog_miss': bool(body.get('catalog_miss', False))})


def _targets(action):
    return tuple(None if line.action == 'add' else line.target_number for line in action.basket.lines)


def resolved_change(value, basket):
    """Bind a proposal, or keep an incomplete one for the handler to reject."""
    action = change_action(value)
    incomplete = (action.basket.unresolved or action.basket.catalog_miss
                  or any(line.unresolved for line in action.basket.lines))
    items = basket.items if hasattr(basket, 'items') else basket
    if incomplete or not action.basket.lines:
        return ResolvedAction(action, basket_targets=_targets(action))
    try:
        return resolve_action(action, basket=list(items))
    except NeedsClarification as exc:
        action.basket.unresolved = [str(exc)]
        return ResolvedAction(action, basket_targets=_targets(action))


def execute_change(value, basket, api_key, *, declared_constraints=()):
    return apply_proposal(resolved_change(value, basket), basket, api_key,
                          declared_constraints=declared_constraints)


def show_cart_action():
    return ActionProposal(kind='SHOW_CART')


def checkout_action(text):
    """Map a canonical checkout value to the action the classifier now emits."""
    raw = (text or '').strip()
    lowered = raw.lower()
    if lowered in {'checkout', 'check out', 'resume checkout'}:
        return ActionProposal(kind='CONTINUE_CHECKOUT')
    mode = mode_request(raw)
    if mode:
        return ActionProposal(kind='SET_FULFILLMENT', value=mode)
    if lowered in {'cash', 'online'}:
        return ActionProposal(kind='SET_PAYMENT_METHOD', value=lowered)
    if CONFIRM.fullmatch(raw):
        return ActionProposal(kind='CONFIRM_ORDER')
    labelled = LABELLED_FIELD.fullmatch(raw)
    if labelled:
        field = FIELD_NAMES.get(labelled[1].lower(), labelled[1].lower())
        return ActionProposal(kind='SET_CHECKOUT_FIELD', field=field, value=labelled[2].strip())
    if lowered.startswith('discount '):
        return ActionProposal(kind='SET_CHECKOUT_FIELD', field='discount_code', value=raw[9:].strip())
    if lowered in {'asap', 'as soon as possible'}:
        return ActionProposal(kind='CLEAR_CHECKOUT_FIELD', field='scheduled_at')
    scheduled = SCHEDULED.fullmatch(raw)
    if scheduled:
        return ActionProposal(kind='SET_CHECKOUT_FIELD', field='scheduled_at', value=scheduled[1])
    return None


def implied_action(query, intent, sub_intent):
    if intent != 'placing_order':
        return None
    if sub_intent == 'check_order_cart':
        return show_cart_action()
    if sub_intent in {'order_confirmation', 'order_payment'}:
        return checkout_action(query)
    return None
