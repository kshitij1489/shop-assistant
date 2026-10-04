"""Plan and attest fixture routes before any model or application turn runs."""
from chatbot_core.capabilities import CAPABILITIES, CONTROL_ROUTES
from chatbot_core.logic.cafe.workflow.actions import execution_route_closure
from chatbot_core.runtime_configuration import RuntimeConfiguration
from evaluate.contracts.interfaces import Blocked


def required_routes(scenario, knowledge):
    routes = {(intent, topic) for intent, topic in knowledge
              if intent in CAPABILITIES and CAPABILITIES[intent].supports(topic)}
    for turn in scenario.turns:
        routes.add((turn.intent, turn.sub_intent))
        routes.update((part['intent'], part['sub_intent']) for part in turn.parts)
    routes.update(CONTROL_ROUTES)
    routes.add(('out_of_context', 'out_of_scope'))
    return execution_route_closure(routes)


def attest_publication(tenant, publication, required):
    configuration = RuntimeConfiguration(str(tenant.pk), tenant.api_key, tenant.slug,
                                         publication.version, publication.documents)
    missing = sorted(route for route in required if not configuration.allows(*route))
    if missing:
        raise Blocked('Fixture capability coverage missing: ' +
                      ', '.join('/'.join(route) for route in missing))
    enabled = {(doc['intent'], doc['sub_intent']) for doc in publication.documents
               if doc['dtype'] == 'intent_classification'
               and configuration.allows(doc['intent'], doc['sub_intent'])}
    return {'required_routes': [list(route) for route in sorted(required)],
            'enabled_routes': [list(route) for route in sorted(enabled)],
            'implicit_control_routes': [list(route) for route in sorted(CONTROL_ROUTES)]}
