"""Legacy published fixture used by existing business/workflow regression tests."""
from chatbot_core.capabilities import CAPABILITIES
from chatbot_core.models import TenantRuntimeConfiguration


def install_runtime_fixture(testcase, *, synthetic=True):
    """Use a published bundle, including the explicit synthetic test capability."""
    from unittest.mock import patch
    testcase.enterContext(patch("chatbot_core.logic.cafe.catalog.load_catalog", return_value={}))
    from chatbot_core.capabilities import Capability
    from chatbot_core.runtime_configuration import RuntimeConfiguration
    scripted = Capability('scripted', 'ScriptedIntent', frozenset({
        'add', 'update', 'ask', 'handoff', 'confirm', 'save', 'fail', 'large', 'yes', 'pending',
    }))
    if synthetic:
        testcase.enterContext(patch.dict(CAPABILITIES, {'scripted': scripted, 'other_type': scripted}))
    documents = [{'dtype': 'intent_classification', 'intent': name, 'sub_intent': topic,
                  'payload': {'description': topic, 'enabled': True}}
                 for name, capability in CAPABILITIES.items() for topic in capability.sub_intents]
    testcase.enterContext(patch('chatbot_core.runtime_configuration.get_configuration',
        side_effect=lambda **kw: RuntimeConfiguration(str(kw.get('tenant_id', 1)),
            kw.get('api_key', 'tenant-1'), 'test-cafe', 1, documents)))


def enable_legacy_capabilities(tenant):
    # Equivalent to migration bootstrap for an already configured deployment.
    documents = [{"dtype": "intent_classification", "intent": name, "sub_intent": topic, "payload": topic}
                 for name, capability in CAPABILITIES.items() for topic in capability.sub_intents]
    TenantRuntimeConfiguration.objects.create(tenant=tenant, version=1, documents=documents)
    from tests.support.ordering import seed_evaluation_policy
    seed_evaluation_policy(tenant)


def classification_result(rows, *, declared_constraints=()):
    """Build the real classifier output from compact scripted routing fixtures.

    A sixth tuple element is an explicit action. Checkout and cart routes
    receive the action their canonical query now requires.
    """
    from chatbot_core.llm.schemas import ClassifiedMessages, IntentClassification
    from tests.support.actions import implied_action
    classifications = []
    for row in rows:
        query, intent, sub, reply_to, clarification = row[:5]
        action = row[5] if len(row) > 5 else implied_action(query, intent, sub)
        classifications.append(IntentClassification(
            query=query, intent=intent, sub_intent=sub, reply_to=reply_to,
            clarification=clarification, action=action))
    return ClassifiedMessages(classifications=classifications,
        declared_constraints=list(declared_constraints))


def classification_rows(result):
    return [(row.query, row.intent, row.sub_intent, row.reply_to, row.clarification)
            for row in result.classifications]
