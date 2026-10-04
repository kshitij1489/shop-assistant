"""One admission policy for restored, newly created and checkout-blocking tasks."""
from pydantic import ValidationError

from chatbot_core.capabilities import CONTROL_ROUTES
from chatbot_core.llm.schemas import OrderProposal
from chatbot_core.logic.action_resolver import TerminalRejection, validate_basket_targets
from chatbot_core.logic.outcomes import TaskOutcome
from chatbot_core.runtime_configuration import active_configuration


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
