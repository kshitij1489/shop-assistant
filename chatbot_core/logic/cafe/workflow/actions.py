"""Adapt universal resolved actions to the existing store business handlers."""
from chatbot_core.logic.action_resolver import NeedsClarification, competing_catalog_items, resolve_action
from chatbot_core.llm.schemas import ActionProposal
import re


ROUTES = {
    'CHANGE_BASKET': ('placing_order', 'update_order'),
    'SHOW_CART': ('placing_order', 'check_order_cart'),
    'SELECT_ADDRESS': ('location_based', 'choose_delivery_address'),
    'SET_FULFILLMENT': ('placing_order', 'order_channels_and_modes'),
    'SET_PAYMENT_METHOD': ('placing_order', 'order_confirmation'),
    'SET_CHECKOUT_FIELD': ('placing_order', 'order_confirmation'),
    'CLEAR_CHECKOUT_FIELD': ('placing_order', 'order_confirmation'),
    'CONTINUE_CHECKOUT': ('placing_order', 'order_confirmation'),
    'CONFIRM_ORDER': ('placing_order', 'order_confirmation'),
    'RECOVER_PAYMENT': ('placing_order', 'order_payment'),
    'CANCEL_PENDING_ACTION': ('general', 'cancel_and_abort'),
}

# Semantic route labels may execute through these canonical action routes.
# This is a provisioning contract, not permission to bypass an unpublished route.
# Keep it separate from action_route(): the model's action remains authoritative
# at execution time, even when it disagrees with its classification label.
EXECUTION_DEPENDENCIES = {
    ('placing_order', 'initiate_order'): {
        ('placing_order', 'add_to_basket'), ROUTES['CONTINUE_CHECKOUT']},
    ('placing_order', 'customize_confirmation'): {
        ('placing_order', 'add_to_basket'), ('placing_order', 'delete_entry'),
        ROUTES['CHANGE_BASKET'], ROUTES['CONTINUE_CHECKOUT']},
    ('placing_order', 'reorder_or_repeat'): {('placing_order', 'add_to_basket')},
    ('placing_order', 'order_payment'): {ROUTES['CONTINUE_CHECKOUT']},
    ('placing_order', 'order_channels_and_modes'): {ROUTES['CONTINUE_CHECKOUT']},
    ('placing_order', 'order_confirmation'): {ROUTES['SET_FULFILLMENT']},
    ('placing_order', 'cancel_and_abort'): {ROUTES['CANCEL_PENDING_ACTION']},
    ('location_based', 'add_delivery_address'): {('location_based', 'confirm_delivery_address')},
    ('location_based', 'update_delivery_address'): {('location_based', 'confirm_delivery_address')},
    ('location_based', 'choose_delivery_address'): {('location_based', 'confirm_delivery_address')},
}


def execution_route_closure(routes):
    """Include action dispatch and handler handoffs needed by advertised routes."""
    result = set(routes)
    pending = list(result)
    while pending:
        for dependency in EXECUTION_DEPENDENCIES.get(pending.pop(), ()):
            if dependency not in result:
                result.add(dependency)
                pending.append(dependency)
    return result


def ordering_evidence(pending, query, rephrased_sentence=None):
    """Use the complete interpreted request for lexical matching across languages.

    Original messages remain stored evidence. Historical decisions without a
    rewrite retain the original accumulation behavior.
    """
    if rephrased_sentence:
        return rephrased_sentence
    if pending is not None and pending.rephrased_sentence and not query:
        return pending.rephrased_sentence
    if pending is None:
        return query
    parts = [pending.basket_item.get('original_request') or pending.original_query or '',
             *pending.basket_item.get('replies', []), query]
    return ' '.join(dict.fromkeys(part for part in parts if part))


def address_entities(state):
    """Saved addresses as resolver entities: stable IDs with their customer labels."""
    return [{'id': row['id'], 'name': row['label'] or ''} for row in state.get('saved_addresses', [])]


def bind_action(proposal, state, *, catalog=(), text=''):
    resolved = resolve_action(proposal, basket=state['basket'].items,
        addresses=address_entities(state),
        focus=state['checklist'].get('basket_focus'),
        checkout=state['checklist'].get('checkout'),
        placed=bool(state['checklist'].get('order') or state['checklist'].get('order_id')),
        catalog=catalog, text=text)
    if (resolved.quote_fingerprint is not None
            and resolved.quote_fingerprint != state.get('offered_quote_fingerprint')):
        raise NeedsClarification('Please review the updated total before confirming the order.')
    return resolved


def action_route(proposal):
    if proposal.kind == 'CHANGE_BASKET' and proposal.basket:
        actions = {line.action for line in proposal.basket.lines}
        if actions == {'add'}:
            return 'placing_order', 'add_to_basket'
        if actions == {'remove'}:
            return 'placing_order', 'delete_entry'
    return ROUTES[proposal.kind]


def required_routes(proposal):
    if proposal.kind == 'CHANGE_BASKET' and proposal.basket:
        topics = {'add': 'add_to_basket', 'remove': 'delete_entry',
                  'update': 'update_order', 'replace': 'update_order'}
        return {('placing_order', topics[line.action]) for line in proposal.basket.lines}
    return {action_route(proposal)}


def explicit_checkout_action(text):
    """The labelled field protocol is authoritative over a contextual rephrase."""
    match = re.fullmatch(r'(name|phone|address|postal code|table(?: id)?):\s*(\S.*)', text.strip(), re.I)
    if match:
        field = {'postal code': 'postal_code', 'table': 'table_id', 'table id': 'table_id'}.get(
            match[1].lower(), match[1].lower())
        return ActionProposal(kind='SET_CHECKOUT_FIELD', field=field, value=match[2])
    value = text.strip().casefold()
    if value in {'pickup', 'delivery', 'dine_in'}:
        return ActionProposal(kind='SET_FULFILLMENT', value=value)
    if value in {'cash', 'online'}:
        return ActionProposal(kind='SET_PAYMENT_METHOD', value=value)
    return None


def compatible_followup(proposal, route, pending, *, catalog=()):
    """A model-supplied reply ID does not give another task ownership of a value."""
    if route == ('general', 'cancel_and_abort'):
        return True
    if (pending.basket_item.get('checkout') or pending.intent_type == 'placing_order'
            and pending.sub_intent in {'order_confirmation', 'order_payment'}):
        if route[0] == 'location_based' and route[1] in {
                'confirm_delivery_address', 'deny_delivery_address',
                'add_delivery_address', 'update_delivery_address'}:
            return True
        return bool(proposal and proposal.kind in {
            'SET_FULFILLMENT', 'SET_PAYMENT_METHOD', 'SET_CHECKOUT_FIELD',
            'CLEAR_CHECKOUT_FIELD', 'CONTINUE_CHECKOUT', 'CONFIRM_ORDER', 'RECOVER_PAYMENT'})
    if pending.intent_type == 'placing_order' and pending.sub_intent in pending.ITEM_ACTIONS:
        if not proposal or proposal.kind != 'CHANGE_BASKET' or not proposal.basket:
            return False
        saved = pending.basket_item.get('action_proposal', {}).get('basket') or pending.basket_item.get('proposal', {})
        operations = {line['action'] for line in saved.get('lines', [])}
        if not operations:
            operations = {'add'} if pending.sub_intent in {'initiate_order', 'add_to_basket'} else (
                {'remove'} if pending.sub_intent == 'delete_entry' else {'update', 'replace'})
        additions = [line for line in saved.get('lines', []) if line['action'] == 'add']
        if additions:
            incoming_items = {line.item_id for line in proposal.basket.lines if line.action == 'add'}
            # A reply may fill an unresolved product choice, size or quantity.
            # Another product's add must not complete this unfinished request,
            # even when the model attaches its task ID. Keep all known products
            # of a multi-item pending operation, too.
            # A guessed product ID may have prompted an item-choice question.
            # Reuse the original wording's catalog candidates so answering that
            # question with a different offered product remains a continuation.
            evidence = ordering_evidence(pending, '')
            choices = [{line['item_id'], *(row['id'] for row in competing_catalog_items(
                evidence, line['item_id'], catalog))} for line in additions if line.get('item_id')]
            if any(not candidates & incoming_items for candidates in choices):
                return False
            if all(line.get('item_id') for line in additions) and not incoming_items <= set().union(*choices):
                return False
        return {line.action for line in proposal.basket.lines} <= operations
    if pending.intent_type == 'location_based' and route[0] == 'location_based':
        # Address collection and its confirmation are one task; coverage and
        # other location questions cannot consume its outstanding fields.
        address_routes = {'add_delivery_address', 'update_delivery_address',
                          'choose_delivery_address', 'confirm_delivery_address', 'deny_delivery_address'}
        if route[1] == pending.sub_intent:
            return True
        if pending.sub_intent in {'set_default_delivery_address', 'verify_address_for_delivery'}:
            return route[1] in {pending.sub_intent, 'add_delivery_address',
                                'update_delivery_address', 'confirm_delivery_address'}
        return pending.sub_intent in address_routes and route[1] in address_routes
    if pending.intent_type == 'placing_order':
        return route == (pending.intent_type, pending.sub_intent)
    return route[0] == pending.intent_type


def requires_action(intent, topic):
    return (intent == 'placing_order' and topic in {
        'initiate_order', 'add_to_basket', 'update_order', 'delete_entry',
        'order_confirmation', 'order_payment', 'customize_confirmation',
    }) or (intent == 'location_based' and topic == 'choose_delivery_address')
