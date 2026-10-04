"""Load and check the committed execution plan; importing has no side effects."""
from pathlib import Path
from evaluate.contracts.models import ScenarioPlan
from evaluate.datasets.loader import load_dataset, read_json
from evaluate.fixtures.definitions import FIXTURES, address, fixture_hashes

PLAN_PATH = Path(__file__).with_name('execution.plan.json')


def load_plan(dataset_directory):
    plan = ScenarioPlan.model_validate(read_json(PLAN_PATH))
    if plan.fixture_hashes != fixture_hashes():
        raise ValueError('Reviewed fixture hashes have changed; review and regenerate the plan')
    return plan, load_dataset(Path(dataset_directory), plan)


def readiness(scenario):
    """Shared setup/runtime blockers used by validate, preflight, and live selection."""
    blockers = [b.model_dump() for b in scenario.blockers]
    for action in scenario.actions:
        if action.operation.kind == 'seed_fixture':
            f = FIXTURES[action.operation.fixture_id]
            labels = [address(key)['label'] for key in f.addresses]
            if len(labels) != len(set(labels)):
                blockers.append(dict(code='duplicate_address_label', location=action.requirement_ref,
                    message='Application enforces unique customer/address label; source requires two Home labels.'))
    return blockers


def execution_mapping(dataset_directory):
    plan, bundle = load_plan(dataset_directory)
    return {'version': plan.version, 'cases': [{
        'scenario_id': s.scenario_id, 'source_hash': s.source_hash,
        'profiles': s.setup_profiles, 'setup': s.setup.model_dump(exclude_none=True),
        'clock': s.clock.model_dump(), 'actions': [a.model_dump() for a in s.actions],
        'runtime_blockers': readiness(s),
        'isolation': 'tenant_per_scenario_and_attempt; serialized in-process controls',
    } for s in bundle.scenarios]}
