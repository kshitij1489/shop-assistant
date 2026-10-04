"""Evidence checks for commands and replies to unfinished operations."""
import re

from chatbot_core.llm.schemas import ActionProposal, ClassifiedMessages, IntentClassification
from chatbot_core.logic.action_resolver import NeedsClarification, TerminalRejection, resolve_reference
from .actions import action_route, address_entities, explicit_checkout_action


def explicit_classification(text, pending):
    """Whole-message protocols do not depend on a contextual model rephrase.

    Mixed natural-language requests still go through classification. These exact
    commands retain the usual capability checks and business validation.
    """
    # A labelled value followed by another clause is not a single-field
    # protocol. Let the normal classifier retain all of its operations.
    if re.search(r'[;\n]|\b(?:and|but|then|also)\b', text, re.I):
        return None
    if len(re.findall(r'\b(?:name|phone|address|postal code|table(?: id)?):', text, re.I)) > 1:
        return None
    action = explicit_checkout_action(text)
    command = text.strip().casefold().rstrip('.!')
    if command in {'checkout', 'check out'}:
        action = ActionProposal(kind='CONTINUE_CHECKOUT')
    elif command in {'show basket', 'show cart', 'show my basket', 'show my cart'}:
        action = ActionProposal(kind='SHOW_CART')
    if action is None:
        return None
    if action.kind == 'SET_CHECKOUT_FIELD' and action.field in {'address', 'postal_code'}:
        addresses = [p for p in pending if p.intent_type == 'location_based' and p.sub_intent in {
            'add_delivery_address', 'update_delivery_address', 'confirm_delivery_address'}]
        if len(addresses) > 1:
            return None  # Let classification clarify which address task owns the value.
        if addresses:
            target = addresses[0]
            topic = ('update_delivery_address' if target.sub_intent == 'confirm_delivery_address'
                     else target.sub_intent)
            return ClassifiedMessages(declared_constraints=[], classifications=[IntentClassification(
                query=text, intent='location_based', sub_intent=topic,
                rephrased_sentence=f'Set the delivery {action.field.replace("_", " ")}: {action.value}',
                reply_to=str(target.query_id), clarification=None, action=None)])
    reply_to = next((str(p.query_id) for p in pending if p.basket_item.get('checkout')), None)
    if action.kind == 'SHOW_CART':
        reply_to = None
    intent, sub_intent = action_route(action)
    if action.kind == 'SET_CHECKOUT_FIELD':
        rewrite = f'Set the checkout {action.field.replace("_", " ")}: {action.value}'
    elif action.kind == 'SET_FULFILLMENT':
        rewrite = f'Use {action.value.replace("_", " ")} for this order'
    elif action.kind == 'SET_PAYMENT_METHOD':
        rewrite = f'Use {action.value} payment for this order'
    else:
        rewrite = 'Show the current basket' if action.kind == 'SHOW_CART' else 'Continue checkout'
    return ClassifiedMessages(declared_constraints=[], classifications=[IntentClassification(
        query=text, intent=intent, sub_intent=sub_intent, reply_to=reply_to,
        rephrased_sentence=rewrite,
        clarification=None, action=action)])


def redundant_address_selection(proposal, classified_route, state):
    """Selecting the address already drafted carries no operation of its own.

    The customer's actual answer is the classified location route (for example a
    confirmation or denial); it executes alone and does not require the
    choose_delivery_address capability. An explicit choice keeps its action.
    With an address drafted, a selection without a reference (or by focus) can
    only mean that address.
    """
    if proposal is None or proposal.kind != 'SELECT_ADDRESS':
        return False
    if classified_route[0] != 'location_based' or classified_route[1] == 'choose_delivery_address':
        return False
    current = state['delivery_address'].get('address_id')
    if not current:
        return False
    reference = proposal.reference
    if reference is None or reference.by == 'focus':
        return True
    try:
        selected = resolve_reference(reference, address_entities(state))
    except (NeedsClarification, TerminalRejection):
        return False
    return str(selected) == str(current)


def unsupported_size_answer(proposal, pending, text, catalog):
    """A repaired/changed size needs evidence from this reply, not a guessed ID.

    Compare the saved unapplied proposal, never a contextual rewrite. Catalog
    aliases are accepted. A newly selected product may use its sole variant;
    an already open size question must actually be answered.
    """
    if pending is None or proposal.kind != 'CHANGE_BASKET' or not proposal.basket:
        return False
    saved = pending.basket_item.get('action_proposal', {}).get('basket') or pending.basket_item.get('proposal', {})
    words = re.findall(r'\w+', text.casefold())
    for line in proposal.basket.lines:
        previous = [old for old in saved.get('lines', [])
                    if old.get('item_id') == line.item_id and old.get('action') == line.action]
        if not previous or any(old.get('variant_id') == line.variant_id for old in previous):
            continue
        item = catalog.get(line.item_id, {})
        variant = next((v for v in item.get('variants', []) if v['id'] == line.variant_id), None)
        if variant is None:
            continue  # Business validation will ask for a valid selection.
        def mentioned(label):
            phrase = re.findall(r'\w+', label.casefold())
            return bool(phrase) and any(words[i:i + len(phrase)] == phrase for i in range(len(words)))
        if not any(mentioned(label) for label in [variant['name'], *variant.get('aliases', [])]):
            return True
    return False
