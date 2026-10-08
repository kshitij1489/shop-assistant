"""Verify semantic eval checks independently of live provider availability."""
import json

from django.test import SimpleTestCase

from scripts.evaluate_address_extraction import ROOT, validate_output


class AddressExtractionEvaluationTests(SimpleTestCase):
    def test_resume_rejects_missing_fields_and_lost_street_details(self):
        case = json.loads((ROOT / 'tests/fixtures/address_extraction.json').read_text())[0]
        complete = {**case['expected_fields'],
                    'street_address': 'flat 11, torre 4 cerca de sector 56'}
        self.assertEqual(validate_output(case, complete), [])
        for field in complete:
            with self.subTest(missing=field):
                self.assertTrue(validate_output(case, {k: v for k, v in complete.items() if k != field}))
        for street in ('flat 11, sector 56', 'flat 110, torre 4 cerca de sector 56'):
            with self.subTest(street=street):
                self.assertTrue(validate_output(case, {**complete, 'street_address': street}))

    def test_noop_is_distinct_from_failure_and_context_leakage(self):
        case = {'expected_fields': {}}
        self.assertEqual(validate_output(case, {}), [])
        self.assertTrue(validate_output(case, None))
        self.assertTrue(validate_output(case, {'city': 'Gurugram'}))

    def test_postal_values_cannot_be_replaced_or_inferred(self):
        case = {'expected_fields': {'city': 'gurugram', 'postal_code': '012001'}}
        self.assertEqual(validate_output(case, {'city': 'Gurugram', 'postal_code': '012001'}), [])
        self.assertTrue(validate_output(case, {'city': 'Gurugram', 'postal_code': '122011'}))
        self.assertTrue(validate_output(case, {**case['expected_fields'], 'country': 'India'}))
