"""Read-only published context for item choices and basket confirmations."""
import logging

from chatbot_core.knowledge_retrieval import retrieve_knowledge
from .catalog import catalog_names
from .order_interpreter import exact_candidates

logger = logging.getLogger(__name__)


def ordering_reply_evidence(api_key, facts):
    """Fetch evidence once for identified products; never guess an unresolved item.

    Operational variant labels are not published serving sizes. Use the same
    tenant-scoped retrieval envelope as knowledge answers, including its coverage,
    freshness, and explicit unknowns, rather than inferring facts from catalog gaps.
    Evidence lookup failures must not undo or retry a completed basket operation.
    """
    item_routes = {('placing_order', topic) for topic in (
        'add_to_basket', 'initiate_order', 'update_order', 'insufficient_information_order')}
    requests = [fact for fact in facts
                if tuple(fact.get('effective_route', ())) in item_routes
                and (fact.get('outcome') == 'needs_clarification' or fact.get('basket_changed'))]
    if not requests:
        return None
    try:
        catalog = {row['id']: {**row, 'item_id': row['id']} for row in catalog_names(api_key)}
        subjects = []
        for fact in requests:
            # A classifier may ask for quantity without attaching any action.
            # Exact catalog names/aliases still identify that item; an unresolved
            # pronoun supplies no candidate and cannot acquire one here.
            item_ids = fact.get('requested_item_ids') or exact_candidates(
                fact.get('rephrased_sentence') or fact['query'], catalog)
            items = list(dict.fromkeys(catalog[item_id]['name'] for item_id in item_ids
                                       if item_id in catalog))
            if items:
                subjects.append({
                    'query': fact['query'],
                    'interpretation': fact.get('rephrased_sentence'),
                    'items': items,
                    'outcome': fact['outcome'],
                    'required_details': ['documented unit price and currency',
                                         'published serving size or what cannot be verified'],
                })
        if not subjects:
            return None
        route = requests[0]['effective_route']
        search = '\n'.join(
            f"{', '.join(subject['items'])}: listed price, currency, serving size, portion, volume and weight. "
            f"Request: {subject['interpretation'] or subject['query']}"
            for subject in subjects)
        knowledge = retrieve_knowledge(api_key, *route, search, rephrased_sentence=search)
        if not knowledge or not knowledge.get('payload'):
            return None
        return {'requests': subjects, 'knowledge': knowledge['payload']}
    except Exception:
        logger.exception('Reply evidence lookup failed; retaining verified workflow results')
        return None
