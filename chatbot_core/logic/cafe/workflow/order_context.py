"""One bounded, tenant-scoped payload for contextual classification."""
from copy import deepcopy
import hashlib
import json

from .. import catalog as catalog_store
from ..order_interpreter import interpretation_context, select_candidates, exact_candidates
from chatbot_core.runtime_configuration import active_configuration
from ..prompts.normalize_and_classify import NormalizationClassificationError


def classification_context(history, checklist, *, tenant_id, chat_id, platform,
                           query='', basket=None, pending=(), tenant=None, customer=None,
                           active_pending=None, delivery_address=None):
    def scoped(row):
        return (str(row.get('tenant')) == str(tenant_id) and row.get('chat_id') == chat_id
                and row.get('platform') == platform)

    recent = [row for row in history if isinstance(row, dict) and scoped(row.get('query_obj', {}))]
    context = {'has_placed_order': bool(checklist.get('order') or checklist.get('order_id')),
               'response_language': checklist.get('response_language', 'en'),
               'recent_exchange': [row['exchange'] for row in recent if row.get('exchange')][-6:],
               'last_assistant_message': checklist.get('last_assistant_message', ''),
               'last_assistant_question': checklist.get('last_assistant_question', ''),
               'open_requests': []}
    if recent and recent[-1]['query_obj'].get('is_complete'):
        context['last_completed_request'] = {key: recent[-1]['query_obj'].get(key)
            for key in ('intent_type', 'sub_intent', 'main_query', 'response')}
    # Recent work plus lexical retrieval keeps older, explicitly named tasks
    # reachable. This selects context only; the model owns conversational routing.
    tokens = set(query.casefold().split())
    def relevance(entry):
        index, request = entry
        return (request is active_pending,
                len(tokens & set(str(request.main_query or '').casefold().split())), index)

    for request in pending:
        if not scoped(request.to_dict()):
            raise ValueError('Pending request does not match session scope')
    relevant = [request for _, request in sorted(enumerate(pending), key=relevance, reverse=True)[:12]]
    context['active_pending_id'] = str(active_pending.query_id) if active_pending in relevant else None
    context['other_open_request_count'] = len(pending) - len(relevant)
    for request in relevant:
        context['open_requests'].append({
            'id': str(request.query_id), 'intent': request.intent_type, 'sub_intent': request.sub_intent,
            'outcome': request.outcome.value,
            'query': request.main_query, 'original_message': request.original_query,
            'question': request.get_followup_question(), 'missing_fields': request.missing_fields,
            'details': interpretation_context(request.basket_item),
            'basket_effect_status': ('not_applied' if request.intent_type == 'placing_order'
                                     and request.sub_intent in getattr(request, 'ITEM_ACTIONS', ()) else None),
        })
    basket_lines = deepcopy(basket.items) if basket is not None else []
    context['basket'] = interpretation_context(basket_lines)
    context['basket_focus'] = checklist.get('basket_focus')
    context['declared_constraints'] = checklist.get('declared_constraints', [])
    context['fulfillment_preference'] = checklist.get('fulfillment_preference')
    draft = checklist.get('checkout', {})
    context['checkout'] = {key: deepcopy(draft[key]) for key in ('mode', 'awaiting', 'payment_method') if key in draft}
    context['checkout']['has_quote'] = bool(draft.get('quote'))
    context['checkout']['fields'] = deepcopy(draft.get('fields', {}))
    context['checkout']['confirmation_policy'] = 'contextual_consent_to_current_quote'
    context['location_confirmed'] = bool(checklist.get('location'))
    context['selected_address_id'] = (delivery_address or {}).get('address_id')
    context['address_draft'] = deepcopy(delivery_address or {})
    config = active_configuration()
    if config is not None and str(config.tenant_id) == str(tenant_id):
        context['configuration_version'] = config.version
        context['enabled_capabilities'] = [[doc['intent'], doc['sub_intent']] for doc in config.documents
            if doc['dtype'] == 'intent_classification' and config.allows(doc['intent'], doc['sub_intent'])]
    if tenant is not None:
        catalog = catalog_store.load_catalog(tenant.api_key)
        ids = set(checklist.get('recommended_item_ids', []))
        ids.update(line.get('item_id') for line in basket_lines)
        for request in pending:
            ids.update(line.get('item_id') for line in request.basket_item.get('proposal', {}).get('lines', []))
            action = request.basket_item.get('action_proposal', {})
            ids.update(line.get('item_id') for line in (action.get('basket') or {}).get('lines', []))
        candidates = select_candidates(query, catalog, ids)
        context['catalog_version'] = hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()
        context['catalog'] = interpretation_context(list(candidates.values()))
        context['catalog_complete'] = len(candidates) == len(catalog)
        context['recommendations'] = [{'item_id': key, 'name': catalog[key]['name']}
                                     for key in checklist.get('recommended_item_ids', []) if key in catalog]
    if customer is not None:
        if str(customer.tenant_id) != str(tenant_id):
            raise ValueError('Customer does not match session scope')
        context['locale'] = getattr(customer, 'locale', None)
        # Names/IDs suffice for routing; full contact/address data belongs in extraction.
        context['saved_addresses'] = [{'id': str(row.pk), 'label': row.label}
            for row in customer.addresses.filter(tenant_id=tenant_id).order_by('pk')[:20]]
        if checklist.get('order_id'):
            from uuid import UUID
            from orders.models import Order
            try:
                order_id = UUID(str(checklist['order_id']))
            except ValueError:
                order_id = None
            if order_id:
                order = Order.objects.filter(pk=order_id, tenant_id=tenant_id, customer=customer).first()
                if order:
                    context['order_state'] = {'id': str(order.pk), 'status': order.order_status,
                                              'payment_status': order.payment_status}
    # Fail closed rather than cutting off a condition or silently dropping a
    # required basket/pending item when configured catalog text is oversized.
    if len(json.dumps(context, ensure_ascii=False).encode()) > 64000:
        raise NormalizationClassificationError('Conversation context exceeds the bounded input budget')
    return context


def remember_reply(state, tenant):
    """Persist the actual delivered exchange in the existing history and checklist."""
    if state['history']:
        state['history'][-1]['exchange'] = {'user': state['query'], 'assistant': state['response']}
    state['checklist']['last_assistant_message'] = state['response']
    question = state.get('delivered_question', '')
    if question:
        state['checklist']['last_assistant_question'] = question
    elif not state['pending_queries']:
        state['checklist'].pop('last_assistant_question', None)
    # Only catalog identities actually present in a delivered menu answer qualify.
    if any(row[1] == 'menu_items' for row in state['classifications']):
        catalog = catalog_store.load_catalog(tenant.api_key)
        ids = exact_candidates(state['response'], catalog)
        names = [catalog[key]['name'].casefold() for key in ids]
        state['checklist']['recommended_item_ids'] = [key for key in ids
            if names.count(catalog[key]['name'].casefold()) == 1][:20]
