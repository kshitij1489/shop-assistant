"""Build the committed plan from exact reviewed requirements, never heuristics.

Only maintainers run this after reviewing changed source. Runtime loads the
committed plan and the foundation loader verifies every source/requirement hash.
"""
import json
from pathlib import Path
from evaluate.identity import canonical_hash
from evaluate.contracts.models import ScenarioPlan
from evaluate.fixtures.definitions import fixture_hashes

ROOT = Path(__file__).resolve().parents[2]
REVIEW = 'evaluate/scenarios/README.md'
CAPABILITIES = ['knowledge_import', 'freeze_clock', 'catalog_seed', 'checkout_settings',
                'address_stubs', 'authenticated_website_customer', 'fake_payment_adapter',
                'lookup_control', 'catalog_control', 'payment_control', 'reconnect',
                'set_delivery_fee', 'seed_fixture']

COMMON = {
    'Basket assertions use the seeded QA standard variant and its price. Published serving size and real-world stock remain unknown; synthetic catalog availability is not a live stock count.': 'catalog-contract',
    'Exercise the quantity-clarification branch. If needed for an isolated continuation test, establish the pending question shown in the reference reply; do not infer it from an ordinary add.': 'quantity-branch',
    'The prior-question branch uses the illustrative intervening assistant question; an equivalent response that repeats the original question is acceptable and should be logged as a branch variation, not a factual failure.': 'branch-variation',
}
SPECIAL = {
    's26_two_addresses_then_choose': ['home-office'],
    's36_addresses_default_and_map_pin': ['work-home'],
    's48_payment_detour_then_address': ['home-office'],
    's49_policy_detour_then_cancel_item': ['vanilla-lamington'],
    's115_address_ambiguous_selection': ['home-and-work'],
    's119_foreign_address_id': ['foreign-address'],
    's133_schedule_validation': ['schedule-policy'],
    's134_schedule_clear_and_horizon': ['schedule-policy'],
    's136_new_order_after_terminal': ['terminal-order'],
    's137_cross_customer_recovery': ['foreign-draft'],
    's139_dine_in_end_to_end': ['dine-in-policy'],
    's140_disabled_fulfillment_mode': ['no-dine-in-policy'],
    's146_scoped_order_lookup': ['foreign-order'],
    's150_cross_session_referent': ['foreign-pending'],
}


def lookup(service, outcome, **kw):
    return dict(kind='lookup_control', service=service, outcome=outcome, **kw)


BEFORE = {
    's117_coverage_failure_retry': [[lookup('coverage', 'unavailable')],
                                   [lookup('coverage', 'success')]],
    's122_delivery_online_end_to_end': [[dict(kind='payment_control', operation='capture', amount_minor=102000)]],
    's129_checkout_price_changed': [[dict(kind='catalog_control', item_name='Pistachio Ice Cream', variant_name='QA standard', price_minor=47000)]],
    's130_checkout_item_unavailable': [[dict(kind='catalog_control', item_name='Pistachio Ice Cream', variant_name='QA standard', available=False)]],
    's131_checkout_reconnect': [[dict(kind='reconnect')]],
    's132_online_provider_unavailable': [[dict(kind='payment_control', operation='timeout_after_creation')],
                                        [dict(kind='payment_control', operation='restore_and_reconcile')]],
    's143_quote_config_changed': [[dict(kind='set_delivery_fee', amount_minor=17500)]],
    's144_closing_revalidation': [[dict(kind='freeze_clock', clock=dict(at='2026-09-29T23:20:00+05:30', timezone='Asia/Kolkata'))]],
    's147_classifier_fault_recovery': [[lookup('classification', 'timeout')], [lookup('classification', 'success')]],
}


def build_plan(root=ROOT / 'test_data'):
    data = json.loads((root / 'session_query_sets.json').read_text())
    qa = json.loads((root / 'qa_test_cases.json').read_text())
    plan = json.loads((ROOT / 'evaluate/datasets/baseline.plan.json').read_text())
    plan.update(version='dataset-setup-v2-text-address', supported_capabilities=CAPABILITIES, fixture_hashes=fixture_hashes())
    # Reviewed exception: checkout uses local synthetic stock to meet commerce's
    # reservation contract. Basket-only profiles retain no stock records.
    plan['profiles']['checkout_sandbox']['defaults']['stock'] = 'finite_local'
    plan['profiles']['checkout_sandbox']['review_ref'] = REVIEW
    for namespace, entries in [('sessions', data['sessions']), ('qa', qa)]:
        for source in entries:
            sid = namespace + ':' + source['id']
            review = dict(source_hash=canonical_hash(source), review_ref=REVIEW,
                          overrides={}, actions=[], requirements={})
            def add(ref, text, index, operations):
                ids = []
                for ordinal, operation in enumerate(operations):
                    aid = sid + ':' + ref.strip('/').replace('/', '-') + ':' + str(ordinal)
                    ids.append(aid)
                    review['actions'].append(dict(action_id=aid, scenario_id=sid,
                        original_turn_index=index, requirement_ref=ref, requirement_hash=canonical_hash(text),
                        review_ref=REVIEW, operation=operation))
                review['requirements'][ref] = ids
            for i, text in enumerate(source.get('preconditions', [])):
                key = COMMON.get(text)
                if not key:
                    keys = SPECIAL.get(source['id'], [])
                    if i >= len(keys):
                        raise ValueError('Requirement has no reviewed translation: ' + sid)
                    key = keys[i]
                operations = [dict(kind='seed_fixture', fixture_id=key, fixture_hash=fixture_hashes()[key])] if key else [lookup('reverse_geocoding', 'unavailable')]
                add('/preconditions/' + str(i), text, None, operations)
            for i, action in enumerate(source.get('before_turn', [])):
                add('/before_turn/' + str(i) + '/action', action['action'], action['turn_index'], BEFORE[source['id']][i])
            if source['id'] in ('s133_schedule_validation', 's134_schedule_clear_and_horizon'):
                review['overrides'].update(scheduling=True, horizon_days=7, lead_minutes=30)
            if source['id'] == 's139_dine_in_end_to_end':
                review['overrides'].update(modes=['pickup', 'delivery', 'dine_in'])
            # QA cases deliberately use explicit empty-live-state knowledge setup.
            if namespace == 'qa':
                review['overrides'].update(catalog='none', stock='none', customer='browser_guest',
                                           address_lookup='unavailable', payment='unavailable')
            plan['scenarios'][sid] = review
    return ScenarioPlan.model_validate(plan)


if __name__ == '__main__':
    (Path(__file__).parent / 'execution.plan.json').write_text(build_plan().model_dump_json(indent=2) + '\n')
