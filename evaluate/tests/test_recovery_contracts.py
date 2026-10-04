"""Provider and fixture contracts without HTTP or application database access."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mock_services.commerce_adapter.provider import FakeProvider
from mock_services.commerce_adapter.fixtures import payment_command
from mock_services.location.fixtures import match_forward, validate_fixture
from evaluate.scenarios.runtime import FaultProvider


class RecoveryContractTests(unittest.TestCase):
    def test_before_and_after_commit_faults_have_distinct_recovery_evidence(self):
        with TemporaryDirectory() as directory:
            provider = FakeProvider(Path(directory) / 'provider.sqlite3', 'test-account')
            self.addCleanup(provider.db.close)
            fault = FaultProvider(provider)
            command = payment_command()
            fault.creation_fault = 'before_commit'
            with self.assertRaises(TimeoutError):
                fault.create(command)
            self.assertIsNone(fault.lookup(key=command['idempotency_key']))
            fault.creation_fault = 'after_commit'
            with self.assertRaises(TimeoutError):
                fault.create(command)
            fault.creation_fault = None
            observed = fault.lookup(key=command['idempotency_key'])
            self.assertEqual(observed['data']['payment_id'], command['data']['payment_id'])
            self.assertTrue(observed['data']['checkout_url'])
            self.assertEqual(provider.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0], 1)

    def test_shared_postcode_fixtures_match_components_without_invented_jurisdiction(self):
        fixtures = [validate_fixture({
            'fixture_id': str(number), 'kind': 'forward',
            'match': {'postal_code': '122003', 'address_terms': [f'Flat {number}', 'Sector 45', 'Gurugram']},
            'result': {'formatted_address': f'Flat {number}, Sector 45, Gurugram, Haryana, 122003, India',
                       'components': {'postal_code': '122003'}},
        }) for number in (2, 18)]
        for text in ('Flat 2, Sector 45, Gurugram 122003',
                     'flat 2 sector 45 gurugram haryana 122003 india'):
            self.assertEqual(match_forward(fixtures, text)[0]['fixture_id'], '2')
        for text in ('Flat 22, Sector 45, Gurugram 122003', 'Sector 45, Gurugram 122003',
                     'Flat 2, Sector 45, Gurugram 122004'):
            self.assertIsNone(match_forward(fixtures, text)[0])
        duplicate = deepcopy(fixtures[0])
        duplicate['fixture_id'] = 'other'
        self.assertEqual(match_forward([*fixtures, duplicate], 'Flat 2 Sector 45 Gurugram 122003')[1],
                         'ambiguous_fixture')
