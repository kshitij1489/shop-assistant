"""Basic address validation and normalization."""
from unittest import TestCase


from chatbot_core.logic.cafe import location_utils as utils


class AddressUtilityTests(TestCase):


    def test_requirements_allow_locality_or_sector(self):
        base = {"house_or_flat": "Flat 1", "city": "Delhi", "state": "Delhi", "country": "India", "postal_code": "110001"}
        for area in ({"street_or_locality": "Main Road"}, {"sector_or_phase": "Sector 1"}):
            self.assertEqual(utils.get_missing_address_keys({**base, **area}), [])
        self.assertEqual(utils.get_missing_address_keys(base), [])
        for pin in ["012001", "12345", "1234567", "null", True, "      ", "१२३४५६"]:
            with self.subTest(pin=pin):
                self.assertIn("postal_code", utils.get_missing_address_keys({**base, "postal_code": pin}))

    def test_normalization_is_safe_and_does_not_accept_metadata(self):
        value = {"house_or_flat": " Flat 1 ", "city": " null ", "state": [], "country": True,
                 "postal_code": 110001, "address_id": "untrusted", "latitude": 12}
        self.assertEqual(utils.normalize_address(value), {"house_or_flat": "Flat 1", "postal_code": "110001"})
        self.assertEqual(utils.normalize_address(None), {})
        self.assertEqual(utils.normalize_address([]), {})
        self.assertIn("street_address", utils.get_missing_address_keys({"house_or_flat": "  "}))

    def test_formatting_preserves_prefixes_and_deduplicates(self):
        self.assertEqual(utils.format_address({"house_or_flat": "Flat 4", "street_or_locality": "Sector 4",
                                              "sector_or_phase": "sector 4", "postal_code": 110001}),
                         "Flat 4, Sector 4, 110001")
        self.assertEqual(utils.format_address({"house_or_flat": "4-A"}), "4-A")
        self.assertEqual(utils.format_address(None), "")

    def test_basic_validation_requires_postal_fields_but_no_street_structure(self):
        address = {"street_address": "Blue gate, behind market", "city": "Delhi", "state": "Delhi",
                   "country": "India", "postal_code": "110001"}
        self.assertEqual(utils.get_missing_address_keys(address), [])
        for field in ('city', 'state', 'country'):
            for value in ('', '123', None):
                self.assertIn(field, utils.get_missing_address_keys({**address, field: value}))
        self.assertEqual(utils.format_address(address), 'Blue gate, behind market, Delhi, 110001, India')
        self.assertEqual(utils.comparable_address(address),
                         utils.comparable_address({**address, 'street_address': ' BLUE gate,  behind market '}))
        self.assertNotEqual(utils.comparable_address({**address, 'street_address': 'Tower 4'}),
                            utils.comparable_address({**address, 'street_address': 'Tower 5'}))

    def test_pincode_extraction_rejects_multiple_candidates_and_embedded_digits(self):
        self.assertEqual(utils.extract_pincode("deliver to 110001"), "110001")
        for value in ["110001 or 122001", "x110001", "1100017", "012001"]:
            self.assertIsNone(utils.extract_pincode(value))
