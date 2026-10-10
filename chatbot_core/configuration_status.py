"""Read-only publication diagnostics shared by the dashboard and operators."""
import json

from django.core.exceptions import ValidationError

from chatbot_core.capabilities import CAPABILITIES
from chatbot_core.intent_definitions import KNOWLEDGE_INTENTS, ORDERING_INFORMATION_ROUTES, PURE_INFORMATION_ROUTES
from chatbot_core.runtime_configuration import RuntimeConfiguration, present, validate_documents


def _index(documents):
    return {(doc['dtype'], doc['intent'], doc['sub_intent']): doc['payload'] for doc in documents}


def _state(configuration, indexed, intent, topic, menu_status=None):
    if not configuration.published:
        return 'Not published'
    capability = CAPABILITIES.get(intent)
    if capability is None or not capability.supports(topic):
        return 'Supporting knowledge only'
    if not configuration.allows_information(intent, topic):
        if ('intent_classification', intent, topic) not in indexed and ('knowledge', intent, topic) in indexed:
            return 'Supporting knowledge only'
        return 'Disabled or not configured'
    if intent in KNOWLEDGE_INTENTS or (intent, topic) in ORDERING_INFORMATION_ROUTES:
        if intent == 'menu_items' and menu_status:
            return menu_status
        if not present(indexed.get(('knowledge', intent, topic))):
            return 'No facts saved for this topic'
        if ('intent_classification', intent, topic) not in indexed:
            return 'Facts available with application defaults'
        return 'Facts available'
    return 'Enabled'


def configuration_report(tenant, drafts, publication):
    """Do not confuse a committed bundle with a fully configured café."""
    published = publication.documents if publication and publication.version > 0 else []
    live = RuntimeConfiguration(str(tenant.pk), tenant.api_key, tenant.slug,
                                publication.version if publication else 0, published)
    # Draft state is prospective, not permission to use unpublished facts.
    candidate = RuntimeConfiguration(str(tenant.pk), tenant.api_key, tenant.slug, 1, drafts)
    from commerce.menu_sync import source_for, assert_menu_fresh
    source = source_for(tenant.pk)
    menu_status = None
    if source and source.mode == 'external':
        try:
            assert_menu_fresh(tenant.pk)
            menu_status = 'Synchronized catalog available'
        except ValueError:
            menu_status = 'Synchronized catalog unavailable or stale'
    live_index, draft_index = _index(published), _index(drafts)
    changed = {key for key in live_index.keys() | draft_index.keys()
               if key not in live_index or key not in draft_index or live_index[key] != draft_index[key]}
    topics = {(intent, topic) for _, intent, topic in live_index.keys() | draft_index.keys()}
    topics.update({('information_about_the_cafe', 'location_and_hours'), ('menu_items', 'explore_options')})
    labels = {'knowledge': 'facts', 'intent_classification': 'routing', 'response_intents': 'answer instructions'}
    rows = []
    for intent, topic in sorted(topics):
        changes = [labels.get(dtype, dtype) for dtype, i, t in sorted(changed) if (i, t) == (intent, topic)]
        values = {dtype: payload for (dtype, i, t), payload in live_index.items() if (i, t) == (intent, topic)}
        rows.append({
            'intent': intent, 'topic': topic,
            'live': _state(live, live_index, intent, topic, menu_status),
            'draft': _state(candidate, draft_index, intent, topic, menu_status),
            'changes': ', '.join(changes),
            'published_values': json.dumps(values, ensure_ascii=False, indent=2),
        })
    summaries = []
    limitations = []
    for label, intent, topic in (
            ('Menu questions', 'menu_items', 'explore_options'),
            ('Location and hours', 'information_about_the_cafe', 'location_and_hours')):
        state = _state(live, live_index, intent, topic, menu_status)
        summaries.append({'label': label, 'state': state})
        if not state.startswith(('Facts available', 'Synchronized catalog available')):
            limitations.append(f'{label}: {state.lower()}.')
    ordering_enabled = any(live.allows('placing_order', topic) for topic in CAPABILITIES['placing_order'].sub_intents
                           if ('placing_order', topic) not in PURE_INFORMATION_ROUTES)
    summaries.append({'label': 'Ordering', 'state': 'Configured routes enabled' if ordering_enabled else 'Disabled'})
    if not ordering_enabled:
        limitations.append('Ordering is disabled.')
    try:
        validate_documents(tenant, drafts)
        errors = []
    except ValidationError as exc:
        errors = exc.messages
    return {'changed_count': len(changed), 'rows': rows, 'summaries': summaries,
            'limitations': limitations, 'errors': errors}
