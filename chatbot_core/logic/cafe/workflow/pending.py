"""One admission policy for restored, newly created and checkout-blocking tasks."""
from copy import deepcopy
from pydantic import ValidationError

from chatbot_core.capabilities import CONTROL_ROUTES
from chatbot_core.llm.schemas import OrderProposal
from chatbot_core.logic.action_resolver import TerminalRejection, validate_basket_targets
from chatbot_core.logic.outcomes import TaskOutcome
from chatbot_core.runtime_configuration import active_configuration

# One budget for delivered item/generic clarifications, regardless of their source.
MAX_CLARIFICATION_QUESTIONS = 2
CLARIFICATION_LIMIT_REASON = 'clarification_limit_reached'
CLARIFICATION_LIMIT_REPLY = ('I still couldn’t work out this request, so I haven’t made any changes. '
                             'Please start again with a new, specific request.')


def clarification_progress(intent):
    """Snapshot semantic choices, excluding question wording and conversation text."""
    from ..catalog import positive_integer
    proposal = ((intent.basket_item.get('action_proposal') or {}).get('basket')
                or intent.basket_item.get('proposal') or {})
    return {
        'lines': [{
            'action': line.get('action'),
            'item_id': line.get('item_id'),
            'variant_id': line.get('variant_id'),
            'quantity': positive_integer(line.get('quantity')),
            'modifiers': deepcopy(line.get('modifiers')),
            'unresolved': len(line.get('unresolved') or []),
        } for line in proposal.get('lines', [])],
        'unresolved': len(proposal.get('unresolved') or []),
        'catalog_miss': bool(proposal.get('catalog_miss')),
    }


def made_clarification_progress(previous, current):
    old, new = previous['lines'], current['lines']
    if not old:
        return any(line['item_id'] for line in new)
    if len(old) != len(new) or any(a['action'] != b['action'] for a, b in zip(old, new)):
        return False
    fields = ('item_id', 'variant_id', 'quantity', 'modifiers')
    if any(a[key] is not None and b[key] is None for a, b in zip(old, new) for key in fields):
        return False
    return (any(a[key] is None and b[key] is not None for a, b in zip(old, new) for key in fields)
            or sum(line['unresolved'] for line in new) + current['unresolved']
            < sum(line['unresolved'] for line in old) + previous['unresolved']
            or previous['catalog_miss'] and not current['catalog_miss'])


def has_clarification_budget(intent):
    return (not intent.basket_item.get('checkout')
            and (intent.intent_type == 'insufficient_information'
                 or intent.intent_type == 'placing_order' and intent.sub_intent in
                 intent.ITEM_ACTIONS | {'insufficient_information_order', 'customize_confirmation'}))


def restore_clarification_budget(intent):
    """Seed old saved tasks before this turn can replace their question or proposal."""
    if has_clarification_budget(intent) and 'clarification_budget' not in intent.basket_item:
        count = max(intent.ignored_count, intent.basket_item.get('clarification_questions', 0),
                    len(intent.follow_up_question) if intent.intent_type == 'insufficient_information' else 0)
        intent.basket_item['clarification_budget'] = {
            'delivered': count, 'progress': clarification_progress(intent)}


def spend_clarification(intent):
    """Called only for the selected, delivered question, after all handlers finish."""
    if not has_clarification_budget(intent) or intent.outcome != TaskOutcome.NEEDS_CLARIFICATION:
        return True
    current = clarification_progress(intent)
    budget = intent.basket_item.get('clarification_budget')
    if budget is None:
        # Newly created tasks have no delivered questions yet. Older tasks were
        # migrated in prepare_followup before their proposals were updated.
        count = max(intent.ignored_count, intent.basket_item.get('clarification_questions', 0))
    else:
        count = 0 if made_clarification_progress(budget['progress'], current) else budget['delivered']
    if count >= MAX_CLARIFICATION_QUESTIONS:
        intent.set_outcome(TaskOutcome.TERMINAL_REJECTION, CLARIFICATION_LIMIT_REPLY)
        return False
    intent.ignored_count = count + 1
    intent.basket_item['clarification_budget'] = {'delivered': count + 1, 'progress': current}
    return True


def allowed_intent(intent):
    configuration = active_configuration()
    if configuration is None:
        return (intent.intent_type, intent.sub_intent) in CONTROL_ROUTES
    if intent.basket_item.get('checkout') and not configuration.allows('placing_order', 'order_confirmation'):
        return False
    return configuration.allows(intent.intent_type, intent.sub_intent)


def resumable_pending(intent, state):
    if intent.intent_type == 'placing_order' and intent.sub_intent in intent.STORE_CONTACT_REPLIES:
        intent.set_outcome(TaskOutcome.TERMINAL_REJECTION, intent.STORE_CONTACT_REPLIES[intent.sub_intent])
        return False
    if not intent.outcome.resumable or intent.intent_type == 'out_of_context':
        return False
    ordering = intent.intent_type == 'placing_order'
    item_action = ordering and intent.sub_intent in intent.ITEM_ACTIONS
    checkout = intent.basket_item.get('checkout') or ordering and intent.sub_intent == 'order_confirmation'
    # Payment recovery can remain open after placement; basket/checkout work cannot.
    if state['checklist'].get('order') or state['checklist'].get('order_id'):
        if item_action or checkout:
            intent.set_outcome(TaskOutcome.COMPLETED, intent.response)
            return False
    if not item_action:
        return True
    try:
        if state['basket'].is_empty() and intent.sub_intent in {'delete_entry', 'update_order', 'special_requests'}:
            raise TerminalRejection('Your basket is empty. There is no entry to change or remove.')
        proposal = (intent.basket_item.get('action_proposal') or {}).get('basket')
        proposal = proposal or intent.basket_item.get('proposal')
        if proposal:
            validate_basket_targets(OrderProposal.model_validate(proposal), state['basket'].items,
                                    focus=state['checklist'].get('basket_focus'))
    except TerminalRejection as exc:
        intent.set_outcome(TaskOutcome.TERMINAL_REJECTION, str(exc))
        return False
    except ValidationError:
        # Legacy partial proposals remain data, never executable instructions.
        pass
    return True


def valid_pending(pending, state):
    return [intent for intent in pending if allowed_intent(intent) and resumable_pending(intent, state)]
