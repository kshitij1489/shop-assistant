"""Customer delivery addresses have no geocoding provider dependency."""
from unittest.mock import patch
from django.test import TestCase
from chatbot_core.logic.cafe.intent_handler import location_based as location
from chatbot_core.models import TenantInfo
from orders.models import Customer, CustomerAddress

ADDRESS = {"street_address": "Blue gate, behind market", "city": "Gurugram", "state": "Haryana", "country": "India", "postal_code": "122102"}


class TextAddressWithoutProviderTests(TestCase):
    """The real handler and ORM; address validation never calls a geographic service."""

    def setUp(self):
        super().setUp()
        self.http = self.enterContext(patch("requests.get", side_effect=AssertionError("Unexpected lookup")))
        self.tenant = TenantInfo.objects.create(slug="text-address", display_name="Eval",
                                                meta={"serviceable_pincodes": ["122102"]})
        from tests.support.runtime import enable_legacy_capabilities
        enable_legacy_capabilities(self.tenant)
        self.customer = Customer.objects.create(tenant=self.tenant, name="QA Guest", phone="000")
        self.extract = self.enterContext(patch.object(location, "extract_address_with_gpt", return_value=dict(ADDRESS)))
        self.address, self.checklist = {}, {"order": False, "location": False}

    def add_address(self, query="my address"):
        intent = location.LocationBasedIntent(main_query=query, sub_intent="add_delivery_address",
                                              tenant=self.tenant.id, chat_id="chat")
        reply, _ = intent.process_query({}, self.address, self.checklist, [], "tenant", self.customer)
        return intent, reply

    def test_delivery_saves_text_without_using_configured_provider(self):
        intent, _ = self.add_address()
        self.assertEqual(intent.handoff_overrides["sub_intent"], "confirm_delivery_address")
        saved = CustomerAddress.objects.get()
        self.assertIsNone(saved.location_coordinates)
        self.assertEqual(saved.components, ADDRESS)
        self.http.assert_not_called()

    def test_unserviceable_postal_code_is_rejected_without_provider(self):
        self.extract.return_value = {**ADDRESS, "postal_code": "110001"}
        intent, reply = self.add_address()
        self.assertIn("don’t currently deliver", reply)
        self.assertFalse(intent.is_complete)
        self.assertFalse(CustomerAddress.objects.exists())
        self.http.assert_not_called()
