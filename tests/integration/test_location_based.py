"""Real location handler, ORM and graph; only external I/O is replaced."""
from tests.support.runtime import classification_result
import importlib
from copy import deepcopy
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase, override_settings

from chatbot_core.models import TenantInfo
from orders.models import Customer, CustomerAddress
from chatbot_core.logic.cafe import db_utils
from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler import location_based as location
from chatbot_core.logic.cafe.location_utils import format_address

ADDRESS = {"house_or_flat": "Flat 4", "street_or_locality": "Main Road", "city": "Delhi", "state": "Delhi", "country": "India", "postal_code": "110001"}


class PendingPayment(base.BaseIntent):
    """Only the handoff envelope is in scope; payment must not execute here."""
    ITEM_ACTIONS = frozenset()
    STORE_CONTACT_REPLIES = {}

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.intent_type = "placing_order"

    def process_query(self, *args):
        raise AssertionError("Payment executed before the user answered its prompt")

    def process_followup(self, *args):
        raise AssertionError("Unexpected payment execution")


class LocationFixture:
    def setUp(self):
        self.tenant = TenantInfo.objects.create(slug="one", display_name="One", meta={"serviceable_pincodes": ["110001"]})
        from tests.support.runtime import enable_legacy_capabilities
        enable_legacy_capabilities(self.tenant)
        self.customer = Customer.objects.create(tenant=self.tenant, name="User", phone="123")
        self.extract = self.enterContext(patch.object(location, "extract_address_with_gpt", return_value={}))
        self.http = self.enterContext(patch("requests.get",
                                            side_effect=AssertionError("Delivery must not use a geographic lookup")))
        self.address, self.checklist = {}, {"order": False, "location": False}

    def intent(self, sub="add_delivery_address", query="my address"):
        return location.LocationBasedIntent(main_query=query, sub_intent=sub, tenant=self.tenant.id, chat_id="user")

    def process(self, intent, incoming=None):
        args = ({}, self.address, self.checklist, [], "tenant", self.customer)
        return intent.process_query(*args) if incoming is None else intent.process_followup(incoming, *args)

    def saved(self, *, label="Home", default=True, components=None, customer=None):
        customer = customer or self.customer
        components = deepcopy(ADDRESS if components is None else components)
        return CustomerAddress.objects.create(tenant=customer.tenant, customer=customer, label=label,
                                               address_line=format_address(components), components=components,
                                               location_coordinates={"lat": 27, "lng": 76}, is_default=default)

    def use(self, addr):
        self.address.update(addr.components, address_id=str(addr.id), latitude=27, longitude=76)


@override_settings(GOOGLE_MAPS_API_KEY="offline-maps-key")
class LocationIntentTests(LocationFixture, TestCase):
    def test_extraction_receives_original_reply_and_current_address_draft(self):
        self.address.update(house_or_flat='Flat 4', city='Delhi')
        intent = self.intent('update_delivery_address', 'Set the pincode to 110001')
        intent.original_query = '110001'
        intent.rephrased_sentence = 'Set the delivery pincode to 110001'
        captured = []

        def extract(query, **context):
            captured.append((query, deepcopy(context)))
            return {'postal_code': '110001'}

        self.extract.side_effect = extract
        self.process(intent)
        self.assertEqual(captured, [('Set the pincode to 110001', {
            'original_text': '110001', 'pending': {'house_or_flat': 'Flat 4', 'city': 'Delhi'},
            'rephrased_sentence': 'Set the delivery pincode to 110001',
        })])

    def test_unknown_coverage_does_not_save_update_or_confirm_address(self):
        saved = self.saved(default=False)
        for meta in ({}, {"serviceable_pincodes": None}, {"serviceable_pincodes": "110001"},
                     {"serviceable_pincodes": ["bad"]}, {"serviceable_localities": ["Main Road"]}):
            for sub in ("add_delivery_address", "update_delivery_address", "confirm_delivery_address"):
                with self.subTest(meta=meta, sub=sub):
                    self.tenant.meta = meta
                    self.use(saved)
                    self.checklist.update(location=True, order=True)
                    self.extract.return_value = {} if sub == "confirm_delivery_address" else {**ADDRESS, "house_or_flat": "Flat 9"}
                    intent = self.intent(sub, "yes" if sub == "confirm_delivery_address" else "Flat 9")
                    reply, _ = self.process(intent)
                    self.assertIn("couldn’t verify delivery coverage", reply)
                    self.assertFalse(self.checklist["location"])
                    self.assertFalse(intent.is_complete)
                    self.assertIsNone(intent.handoff_to)
                    saved.refresh_from_db()
                    self.assertEqual(saved.components, ADDRESS)
                    self.assertFalse(saved.is_default)
                    self.assertEqual(CustomerAddress.objects.count(), 1)
        self.http.assert_not_called()

    def test_explicitly_unserviceable_address_is_not_saved_or_confirmed(self):
        saved = self.saved()
        self.tenant.meta = {"serviceable_pincodes": []}
        self.extract.return_value = ADDRESS
        reply, _ = self.process(self.intent())
        self.assertIn("don’t currently deliver", reply)
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.use(saved)
        self.extract.return_value = {}
        reply, _ = self.process(self.intent("confirm_delivery_address", "yes"))
        self.assertIn("don’t currently deliver", reply)
        self.assertFalse(self.checklist["location"])
        self.http.assert_not_called()

    def test_partial_add_keeps_existing_fields_and_has_no_placeholder_reply(self):
        self.extract.return_value = {"house_or_flat": "Flat 4", "city": "Delhi"}
        intent = self.intent()
        reply, followup = self.process(intent)
        self.assertEqual(reply, intent.get_followup_question())
        self.assertIn("pincode", reply)
        self.assertNotIn("333", reply)
        self.assertIsNone(followup)
        self.assertFalse(intent.is_complete)
        self.extract.return_value = {"house_or_flat": None, "city": "", "street_or_locality": "Main Road", "postal_code": 110001, "state": "Delhi", "country": "India"}
        self.process(intent, self.intent(query="Main Road, 110001"))
        self.assertEqual({key: self.address[key] for key in ADDRESS}, ADDRESS)
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.assertEqual(intent.handoff_overrides["sub_intent"], "confirm_delivery_address")
        self.assertFalse(self.checklist["location"])
        self.assertEqual(CustomerAddress.objects.get().components, ADDRESS)
        self.http.assert_not_called()

    def test_new_add_creates_separate_address_instead_of_overwriting_current(self):
        old = self.saved()
        self.use(old)
        self.checklist["location"] = True
        self.extract.return_value = {**ADDRESS, "house_or_flat": "Flat 5"}
        self.process(self.intent())
        old.refresh_from_db()
        self.assertEqual(old.components, ADDRESS)
        self.assertNotEqual(self.address["address_id"], str(old.id))
        self.assertFalse(self.checklist["location"])
        self.assertEqual(CustomerAddress.objects.count(), 2)

    def test_updates_clear_old_coordinates_and_mirror_text(self):
        addr = self.saved()
        self.use(addr)
        self.checklist["location"] = True
        self.extract.return_value = {"house_or_flat": "Flat 5"}
        self.process(self.intent("update_delivery_address", "Flat 5"))
        addr.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertEqual(addr.components["house_or_flat"], "Flat 5")
        self.assertIsNone(addr.location_coordinates)
        self.assertNotIn("lat", self.customer.location_coordinates)
        self.assertNotIn("address_id", addr.components)
        self.assertFalse(self.checklist["location"])

    def test_failed_postal_correction_cannot_confirm_stale_saved_address(self):
        addr = self.saved()
        self.use(addr)
        self.extract.return_value = {"postal_code": "123"}
        pending = self.intent("update_delivery_address")
        self.process(pending)
        addr.refresh_from_db()
        self.assertEqual(addr.components, ADDRESS)
        self.extract.return_value = {}
        self.process(pending, self.intent("confirm_delivery_address", "yes"))
        self.assertFalse(self.checklist["location"])
        self.assertIsNone(pending.handoff_to)
        self.extract.return_value = {"postal_code": "110001", "house_or_flat": "Flat 5"}
        self.process(pending, self.intent("update_delivery_address", "Flat 5, 110001"))
        self.assertFalse(self.checklist["location"])
        self.assertEqual(pending.handoff_overrides["sub_intent"], "confirm_delivery_address")
        self.extract.return_value = {}
        self.process(pending, self.intent("confirm_delivery_address", "yes"))
        self.assertTrue(self.checklist["location"])
        self.assertEqual(CustomerAddress.objects.count(), 1)
        addr.refresh_from_db()
        self.assertEqual(addr.components["house_or_flat"], "Flat 5")

    def test_free_form_street_needs_no_house_sector_or_gps(self):
        self.extract.return_value = {"street_address": "Blue gate behind the market, Tower 4",
                                     "city": "Delhi", "state": "Delhi", "country": "India", "postal_code": "110001"}
        self.process(self.intent())
        saved = CustomerAddress.objects.get()
        self.assertEqual(saved.components, self.extract.return_value)
        self.assertIsNone(saved.location_coordinates)
        self.assertNotIn("latitude", self.address)
        self.extract.return_value = {}
        self.process(self.intent("confirm_delivery_address", "yes"))
        self.assertTrue(self.checklist["location"])
        self.http.assert_not_called()

    def test_empty_extraction_and_case_changes_do_not_reconfirm_s183_s193(self):
        addr = self.saved(components={"street_address": "Flat 4, Main Road", "city": "Delhi",
                                      "state": "Delhi", "country": "India", "postal_code": "110001"})
        for reply, delta in [("Да, подтверждаю", {}), ("Sim, confirmo", {}),
                             ("yes", {"street_address": "flat   4, main road", "city": "DELHI"})]:
            with self.subTest(reply=reply):
                self.address.clear()
                self.use(addr)
                self.extract.return_value = delta
                intent = self.intent("confirm_delivery_address", "Confirm Flat 4, Main Road, Delhi 110001")
                intent.original_query = reply
                self.process(intent)
                self.assertTrue(self.checklist["location"])
                self.assertIsNone(intent.handoff_to)
        self.http.assert_not_called()

    def test_extraction_failure_does_not_confirm_or_parse_rewritten_address(self):
        self.use(self.saved())
        self.extract.return_value = None
        reply, _ = self.process(self.intent("confirm_delivery_address", "Confirm Flat 4, Main Road, Delhi 110001"))
        self.assertIn("couldn’t read", reply)
        self.assertFalse(self.checklist["location"])

    def test_invalid_foreign_and_deleted_ids_cannot_be_updated_or_confirmed(self):
        other = Customer.objects.create(tenant=self.tenant, name="Other", phone="456")
        foreign = self.saved(customer=other)
        for address_id in ["bad-id", str(uuid4()), str(foreign.id)]:
            with self.subTest(address_id=address_id):
                self.address = {**ADDRESS, "address_id": address_id}
                self.extract.return_value = {"house_or_flat": "Flat 5"}
                intent = self.intent("update_delivery_address")
                self.process(intent)
                self.assertFalse(intent.is_complete)
                self.extract.return_value = {}
                self.process(intent, self.intent("confirm_delivery_address", "yes"))
                self.assertFalse(self.checklist["location"])
        foreign.refresh_from_db()
        self.assertEqual(foreign.components, ADDRESS)
        self.http.assert_not_called()

    def test_denial_clears_address_and_location_and_accepts_replacement(self):
        addr = self.saved()
        self.use(addr)
        self.checklist["location"] = True
        pending = self.intent("confirm_delivery_address")
        self.process(pending, self.intent("deny_delivery_address", "no"))
        self.assertEqual(self.address, {})
        self.assertFalse(self.checklist["location"])
        self.assertFalse(pending.is_complete)
        self.assertEqual(pending.sub_intent, "add_delivery_address")
        self.extract.return_value = {**ADDRESS, "house_or_flat": "Flat 7"}
        self.process(pending, self.intent(query="Flat 7"))
        self.assertNotEqual(self.address["address_id"], str(addr.id))
        addr.refresh_from_db()
        self.assertEqual(addr.components, ADDRESS)

    def test_denied_provisional_address_is_replaced_not_duplicated_s31(self):
        self.extract.return_value = {**ADDRESS, "house_or_flat": "Flat 2"}
        pending = self.intent()
        self.process(pending)
        first = CustomerAddress.objects.get()
        self.assertTrue(self.address["provisional"])
        self.process(pending, self.intent("deny_delivery_address", "No, that's the old flat"))
        self.assertEqual(self.address, {"replace_address_id": str(first.id)})
        self.assertFalse(self.checklist["location"])
        self.extract.return_value = {**ADDRESS, "house_or_flat": "Flat 18"}
        self.process(pending, self.intent(query="Flat 18, Main Road"))
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.assertEqual(self.address["address_id"], str(first.id))
        self.assertEqual(pending.handoff_overrides["sub_intent"], "confirm_delivery_address")
        self.extract.return_value = {}
        self.process(pending, self.intent("confirm_delivery_address", "yes"))
        self.assertTrue(self.checklist["location"])
        self.assertNotIn("provisional", self.address)
        first.refresh_from_db()
        self.assertEqual(first.components["house_or_flat"], "Flat 18")

    def test_fresh_add_over_a_provisional_draft_reuses_its_row(self):
        self.extract.return_value = ADDRESS
        self.process(self.intent())
        first = CustomerAddress.objects.get()
        self.extract.return_value = {**ADDRESS, "house_or_flat": "Flat 9"}
        self.process(self.intent())
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.assertEqual(self.address["address_id"], str(first.id))
        first.refresh_from_db()
        self.assertEqual(first.components["house_or_flat"], "Flat 9")

    def test_confirmed_or_saved_addresses_are_never_replaced_by_a_denial(self):
        saved = self.saved()
        self.use(saved)
        self.extract.return_value = {}
        self.process(self.intent("confirm_delivery_address", "yes"))
        self.assertTrue(self.checklist["location"])
        pending = self.intent("confirm_delivery_address")
        self.process(pending, self.intent("deny_delivery_address", "no"))
        self.assertEqual(self.address, {})
        self.extract.return_value = {**ADDRESS, "house_or_flat": "Flat 7"}
        self.process(pending, self.intent(query="Flat 7"))
        self.assertEqual(CustomerAddress.objects.count(), 2)
        saved.refresh_from_db()
        self.assertEqual(saved.components, ADDRESS)

    def test_unsaved_complete_draft_is_resubmitted_when_coverage_recovers_s117(self):
        self.tenant.meta = {}
        self.extract.return_value = ADDRESS
        pending = self.intent()
        reply, _ = self.process(pending)
        self.assertIn("couldn’t verify delivery coverage", reply)
        self.assertEqual(CustomerAddress.objects.count(), 0)
        self.extract.return_value = {}
        reply, _ = self.process(pending, self.intent(query="Yes, use it anyway"))
        self.assertIn("couldn’t verify delivery coverage", reply)
        self.tenant.meta = {"serviceable_pincodes": ["110001"]}
        self.process(pending, self.intent(query="check again"))
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.assertEqual(pending.handoff_overrides["sub_intent"], "confirm_delivery_address")
        self.assertFalse(self.checklist["location"])

    def test_unchanged_address_reply_during_confirmation_confirms(self):
        self.extract.return_value = ADDRESS
        self.process(self.intent())
        pending = self.intent("confirm_delivery_address")
        self.process(pending, self.intent(query="Yes, use the provided address: Flat 4, Main Road"))
        self.assertTrue(self.checklist["location"])
        self.assertTrue(pending.is_complete)
        self.assertEqual(CustomerAddress.objects.count(), 1)

    def test_no_op_update_of_a_saved_address_still_asks_for_the_change(self):
        self.use(self.saved())
        self.extract.return_value = {}
        intent = self.intent("update_delivery_address", "update my Home address")
        reply, _ = self.process(intent)
        self.assertIn("want to add or change", reply)
        self.assertIsNone(intent.handoff_to)

    def test_correction_during_confirmation_requires_fresh_confirmation(self):
        addr = self.saved()
        self.use(addr)
        self.extract.return_value = {"house_or_flat": "Flat 5"}
        for sub in ["update_delivery_address", "add_delivery_address", "confirm_delivery_address"]:
            with self.subTest(sub=sub):
                self.address["house_or_flat"] = "Flat 4"
                pending = self.intent("confirm_delivery_address")
                self.process(pending, self.intent(sub, "actually Flat 5"))
                self.assertFalse(self.checklist["location"])
                self.assertEqual(pending.handoff_overrides["sub_intent"], "confirm_delivery_address")
                self.assertEqual(self.address["house_or_flat"], "Flat 5")

    def test_confirm_valid_saved_address_works_with_missing_order_checklist(self):
        addr = self.saved()
        self.use(addr)
        self.checklist.clear()
        intent = self.intent("confirm_delivery_address", "yes")
        reply, _ = self.process(intent)
        self.assertIn("Delivery address confirmed", reply)
        self.assertNotIn("ship", reply)
        self.assertTrue(self.checklist["location"])
        self.assertTrue(intent.is_complete)
        self.http.assert_not_called()

    def test_complete_unsaved_address_is_saved_and_asked_for_confirmation_first(self):
        self.address.update(ADDRESS)
        intent = self.intent("confirm_delivery_address", "yes")
        self.process(intent)
        self.assertFalse(self.checklist["location"])
        self.assertEqual(intent.handoff_to, "location_based")
        self.assertEqual(CustomerAddress.objects.count(), 1)

    def test_order_payment_is_only_handed_off_after_confirmation(self):
        addr = self.saved()
        self.use(addr)
        self.checklist["order"] = True
        intent = self.intent("confirm_delivery_address", "yes")
        self.process(intent)
        self.assertTrue(self.checklist["location"])
        self.assertEqual(intent.handoff_to, "placing_order")
        self.assertEqual(intent.handoff_overrides["sub_intent"], "order_payment")
        self.assertTrue(intent.is_complete)

    def test_choose_without_saved_addresses_starts_add(self):
        intent = self.intent("choose_delivery_address", "choose address")
        self.process(intent)
        self.assertEqual(intent.sub_intent, "add_delivery_address")

    def test_choose_address_lists_then_loads_selection_and_confirms(self):
        addr = self.saved()
        pending = self.intent("choose_delivery_address", "choose an address")
        self.process(pending)
        self.assertFalse(pending.is_complete)
        self.assertIn(str(addr.id), pending.get_followup_question())
        self.process(pending, self.intent("confirm_delivery_address", "Home"))
        self.assertEqual(self.address["address_id"], str(addr.id))
        self.assertEqual(pending.handoff_to, "location_based")
        self.assertFalse(self.checklist["location"])

    def test_default_selection_updates_profile_and_active_session(self):
        first = self.saved()
        second = self.saved(label="Work", default=False, components={**ADDRESS, "house_or_flat": "Flat 8"})
        self.use(first)
        pending = self.intent("set_default_delivery_address", "set my default")
        self.process(pending)
        self.process(pending, self.intent(query=str(second.id)))
        self.assertTrue(pending.is_complete)
        self.assertEqual(self.address["address_id"], str(second.id))
        first.refresh_from_db()
        second.refresh_from_db()
        self.customer.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(second.is_default)
        self.assertEqual(second.components["house_or_flat"], "Flat 8")
        self.assertEqual(self.customer.location_coordinates["address"], second.address_line)

    def test_selection_does_not_pick_an_arbitrary_or_foreign_address(self):
        first = self.saved()
        second = self.saved(label="Work", default=False)
        for query in ["unknown", str(uuid4()), "Home or Work", f"{first.id} or {second.id}"]:
            intent = self.intent("set_default_delivery_address", query)
            self.process(intent)
            self.assertFalse(intent.is_complete)
            self.assertEqual(self.address, {})

    def test_existing_addresses_returns_every_saved_address(self):
        first = self.saved()
        second = self.saved(label="Work", default=False)
        intent = self.intent("existing_addresses")
        reply, _ = self.process(intent)
        self.assertIn(str(first.id), reply)
        self.assertIn(str(second.id), reply)
        self.assertTrue(intent.is_complete)
        self.assertEqual(intent.follow_up_question, [])

    def test_delivery_check_uses_new_pincode_reply_and_handles_all_verdicts(self):
        pending = self.intent("verify_address_for_delivery", "can you deliver?")
        self.process(pending)
        self.assertFalse(pending.is_complete)
        reply, _ = self.process(pending, self.intent(query="110001"))
        self.assertIn("Yes", reply)
        self.assertTrue(pending.is_complete)
        reply, _ = self.process(self.intent("verify_address_for_delivery", "122001"))
        self.assertIn("don’t currently", reply)
        self.tenant.meta = {}
        reply, _ = self.process(self.intent("verify_address_for_delivery", "110001"))
        self.assertIn("couldn’t verify", reply)
        self.assertEqual(self.address, {})

    def test_coverage_does_not_confuse_locality_and_pincode_configuration(self):
        self.tenant.meta = {"serviceable_localities": ["Main Road"]}
        reply, _ = self.process(self.intent("verify_address_for_delivery", "110001"))
        self.assertIn("couldn’t verify", reply)
        self.tenant.meta["serviceable_pincodes"] = ["110001"]
        reply, _ = self.process(self.intent("verify_address_for_delivery", "deliver to Main Road"))
        self.assertIn("Yes", reply)
        self.assertTrue(db_utils.verify_delivery_pincode(self.tenant, "Main Road"))

    def test_missing_customer_all_branches_and_unknown_intent_are_safe(self):
        self.customer = None
        for sub in location.LocationBasedIntent.SUB_INTENT_NAMES:
            with self.subTest(sub=sub):
                intent = self.intent(sub)
                reply, _ = self.process(intent)
                self.assertIn("profile", reply)
                self.assertTrue(intent.is_complete)
        with self.assertLogs(location.logger, "WARNING"):
            self.assertIn("specify", self.process(self.intent("unknown"))[0])
        self.extract.assert_not_called()
        self.http.assert_not_called()

    def test_cross_tenant_customer_is_rejected(self):
        intent = self.intent()
        intent.tenant = self.tenant.id + 100
        with self.assertRaisesRegex(ValueError, "tenant"):
            self.process(intent)
        self.assertEqual(CustomerAddress.objects.count(), 0)

    def test_delete_is_supported_without_mutating_saved_addresses(self):
        self.saved()
        intent = self.intent("delete_delivery_address")
        self.assertIn("app", self.process(intent)[0])
        self.assertTrue(intent.is_complete)
        self.assertEqual(CustomerAddress.objects.count(), 1)

    def test_instances_have_independent_mutable_state(self):
        first, second = self.intent(), self.intent()
        first.follow_up_question.append("question")
        first.basket_item["name"] = "coffee"
        self.assertEqual(second.follow_up_question, [])
        self.assertEqual(second.basket_item, {})

    def test_unserviceable_pincode_cannot_be_saved_or_confirmed_for_payment(self):
        self.extract.return_value = {**ADDRESS, "postal_code": "122001"}
        reply, _ = self.process(self.intent())
        self.assertIn("don’t currently deliver", reply)
        self.assertEqual(CustomerAddress.objects.count(), 0)
        self.http.assert_not_called()
        addr = self.saved(components={**ADDRESS, "postal_code": "122001"})
        self.address.clear()
        self.use(addr)
        self.extract.return_value = {}
        self.checklist["order"] = True
        pending = self.intent("confirm_delivery_address", "yes")
        self.process(pending)
        self.assertFalse(self.checklist["location"])
        self.assertFalse(pending.is_complete)
        self.assertIsNone(pending.handoff_to)

    def test_update_targets_explicit_selection_and_rejects_unknown_ids(self):
        first = self.saved()
        work = self.saved(label="Work", default=False)
        self.use(first)
        self.extract.return_value = {"house_or_flat": "Flat 8"}
        self.process(self.intent("update_delivery_address", f"change {work.id} to Flat 8"))
        first.refresh_from_db()
        work.refresh_from_db()
        self.assertEqual(first.components, ADDRESS)
        self.assertEqual(work.components["house_or_flat"], "Flat 8")
        self.assertEqual(self.address["address_id"], str(work.id))
        self.extract.return_value = {"house_or_flat": "Flat 9"}
        self.process(self.intent("update_delivery_address", f"change {uuid4()} to Flat 9"))
        work.refresh_from_db()
        self.assertEqual(work.components["house_or_flat"], "Flat 8")

    def test_ambiguous_pincodes_do_not_fall_back_to_an_unrelated_locality(self):
        self.tenant.meta["serviceable_localities"] = ["Main Road"]
        intent = self.intent("verify_address_for_delivery", "110001 or 122001 on Main Road")
        self.assertIn("one valid", self.process(intent)[0])
        self.assertFalse(intent.is_complete)

    def test_invalid_coverage_configuration_is_unknown_not_a_delivery_rejection(self):
        for value in [None, "110001", [None], [True], ["invalid"]]:
            self.tenant.meta = {"serviceable_pincodes": value}
            self.assertIsNone(db_utils.verify_delivery_pincode(self.tenant, "110001"))
        self.tenant.meta = {"serviceable_pincodes": [110001]}
        self.assertTrue(db_utils.verify_delivery_pincode(self.tenant, 110001))
        self.assertIsNone(db_utils.verify_delivery_pincode(self.tenant, None))
        self.tenant.meta = {"serviceable_pincodes": []}
        self.assertFalse(db_utils.verify_delivery_pincode(self.tenant, "110001"))



@override_settings(GOOGLE_MAPS_API_KEY="offline-maps-key")
class LocationGraphTests(LocationFixture, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")

    def setUp(self):
        super().setUp()
        from chatbot_core.logic.cafe.session import redis_session
        from tests.support.conversations import FakeRedis
        from tests.support.replies import install_reply_renderer
        install_reply_renderer(self)
        self.redis_session = redis_session
        self.enterContext(patch.object(redis_session, "_redis", FakeRedis()))
        self.enterContext(patch.object(self.runner, "get_chat_ongoing_session", return_value=object()))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.classify = self.enterContext(patch.object(self.graph, "normalize_and_classify"))
        # Keep unrelated order parsing/model downloads outside these tests.
        def resolve(name):
            return {"location_based": location.LocationBasedIntent, "placing_order": PendingPayment}[name]
        self.enterContext(patch.object(base, "get_intent", side_effect=resolve))
        self.enterContext(patch.object(self.graph, "get_intent", side_effect=resolve))

    def store(self):
        return self.redis_session.RedisSessionStore("user", tenant_id=self.tenant.id, platform="telegram")

    def send(self, sub, query, extracted=None, clarification=None):
        self.extract.return_value = extracted or {}
        pending, _ = self.store().get_ongoing_queries()
        reply_to = str(pending[-1].query_id) if pending else None
        self.classify.return_value = classification_result([(query, "location_based", sub, reply_to, clarification)])
        return self.runner.run_conversation(self.tenant, self.store(), query, self.customer)[0]

    def pending(self):
        pending, index = self.store().get_ongoing_queries()
        self.assertEqual((len(pending), index), (1, 0))
        return pending[0]

    def test_s114_clarification_preserves_street_and_postal_draft(self):
        self.send("add_delivery_address", "My flat is 8, Tower B.",
                  {"street_address": "Flat 8, Tower B"}, clarification="Please share the city and postal details.")
        self.send("add_delivery_address", "Ramgarh Road, Sector 64, Delhi, Delhi, India. Pincode 11000.",
                  {"street_address": "Flat 8, Tower B, Ramgarh Road, Sector 64", "city": "Delhi",
                   "state": "Delhi", "country": "India", "postal_code": "11000"},
                  clarification="Please share a valid six-digit pincode.")
        draft = self.store().get_delivery_address()
        self.assertEqual(draft["street_address"], "Flat 8, Tower B, Ramgarh Road, Sector 64")
        self.assertEqual(draft["city"], "Delhi")
        self.assertFalse(CustomerAddress.objects.exists())
        reply = self.send("update_delivery_address", "110001", {"postal_code": "110001"})
        self.assertIn("Please confirm", reply)
        self.assertFalse(self.store().get_checklist()["location"])
        self.send("confirm_delivery_address", "Yes")
        self.assertTrue(self.store().get_checklist()["location"])
        self.assertEqual(CustomerAddress.objects.get().components["street_address"], draft["street_address"])

    def test_s163_tower_survives_clarification_and_partial_completion(self):
        self.send("add_delivery_address", "torre 4 cerca de sector 56",
                  {"street_address": "torre 4 cerca de sector 56"}, clarification="Please share city, state, country and pincode.")
        self.send("add_delivery_address", "flat 11, Delhi, Delhi, India 110001",
                  {"street_address": "flat 11, torre 4 cerca de sector 56", "city": "Delhi",
                   "state": "Delhi", "country": "India", "postal_code": "110001"})
        self.send("confirm_delivery_address", "sí, esa dirección está bien")
        self.assertTrue(self.store().get_checklist()["location"])
        self.assertIn("torre 4", CustomerAddress.objects.get().address_line)

    def test_unsupported_pin_clarification_can_recover_with_text(self):
        self.send("add_delivery_address", "latitude 91, longitude 77", {},
                  clarification="Location pins are not supported. Please type your address.")
        self.assertFalse(CustomerAddress.objects.exists())
        self.send("add_delivery_address", "full address", ADDRESS)
        self.send("confirm_delivery_address", "yes")
        self.assertTrue(self.store().get_checklist()["location"])

    def test_reselecting_the_drafted_address_is_the_confirmation_s50(self):
        from chatbot_core.llm.schemas import ActionProposal
        from chatbot_core.models import TenantRuntimeConfiguration
        published = TenantRuntimeConfiguration.objects.get(tenant=self.tenant)
        published.documents = [doc for doc in published.documents if doc["sub_intent"] != "choose_delivery_address"]
        published.version += 1
        published.save()
        self.send("add_delivery_address", "full address", ADDRESS)
        address_id = str(CustomerAddress.objects.get().id)
        pending = self.pending()
        self.assertEqual(pending.sub_intent, "confirm_delivery_address")
        selection = ActionProposal(kind="SELECT_ADDRESS", reference={"by": "id", "value": address_id})
        self.extract.return_value = {}
        unreferenced = ActionProposal(kind="SELECT_ADDRESS", reference=None)
        for action, reply_to in ((selection, str(pending.query_id)), (unreferenced, None)):
            with self.subTest(reference=action.reference):
                checklist = self.store().get_checklist()
                checklist["location"] = False
                self.store().set_checklist(checklist)
                self.classify.return_value = classification_result([
                    ("Yes, that's right", "location_based", "confirm_delivery_address", reply_to, None, action)])
                reply = self.runner.run_conversation(self.tenant, self.store(), "Yes, that's right", self.customer)[0]
                self.assertIn("Delivery address confirmed", reply)
                self.assertTrue(self.store().get_checklist()["location"])
                self.assertEqual(CustomerAddress.objects.count(), 1)
        # An explicit choice still needs the unpublished capability.
        self.classify.return_value = classification_result([
            ("Use that address", "location_based", "choose_delivery_address", None, None, selection)])
        reply = self.runner.run_conversation(self.tenant, self.store(), "Use that address", self.customer)[0]
        self.assertIn("currently unavailable", reply)

    def test_partial_address_correction_rejection_and_confirmation_roundtrip(self):
        reply = self.send("add_delivery_address", "Flat 4", {"house_or_flat": "Flat 4"})
        self.assertEqual(reply.count("Please share"), 1)
        self.assertEqual(self.pending().sub_intent, "add_delivery_address")
        reply = self.send("add_delivery_address", "Main Road Delhi 110001", {key: val for key, val in ADDRESS.items() if key != "house_or_flat"})
        self.assertEqual(reply.count("Please confirm"), 1)
        first_id = self.store().get_delivery_address()["address_id"]
        self.assertEqual(self.pending().sub_intent, "confirm_delivery_address")
        reply = self.send("update_delivery_address", "Flat 5 instead", {"house_or_flat": "Flat 5"})
        self.assertIn("Flat 5", reply)
        self.assertEqual(reply.count("Please confirm"), 1)
        self.assertEqual(self.store().get_delivery_address()["address_id"], first_id)
        self.assertFalse(self.store().get_checklist()["location"])
        reply = self.send("deny_delivery_address", "no")
        self.assertEqual(reply.count("Please share"), 1)
        # The denied row was created by this chat and never confirmed: it is not
        # a selection any more, but the replacement overwrites it.
        self.assertEqual(self.store().get_delivery_address(), {"replace_address_id": first_id})
        self.assertEqual(self.pending().sub_intent, "add_delivery_address")
        self.send("add_delivery_address", "Flat 7 at Main Road", {**ADDRESS, "house_or_flat": "Flat 7"})
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.assertEqual(self.store().get_delivery_address()["address_id"], first_id)
        reply = self.send("confirm_delivery_address", "yes")
        self.assertIn("Delivery address confirmed", reply)
        self.assertTrue(self.store().get_checklist()["location"])
        self.assertNotIn("provisional", self.store().get_delivery_address())
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))
        self.assertEqual(CustomerAddress.objects.get(id=first_id).components["house_or_flat"], "Flat 7")

    def test_payment_handoff_is_queued_with_correct_scope_and_never_executed(self):
        checklist = self.store().get_checklist()
        checklist["order"] = True
        self.store().set_checklist(checklist)
        self.send("add_delivery_address", "address", ADDRESS)
        self.assertFalse(self.store().get_checklist()["location"])
        reply = self.send("confirm_delivery_address", "yes")
        self.assertEqual(reply.strip(), "Ready for payment?")
        pending = self.pending()
        self.assertEqual((pending.intent_type, pending.sub_intent), ("placing_order", "order_payment"))
        self.assertEqual((pending.tenant, pending.chat_id, pending.platform), (self.tenant.id, "user", "telegram"))
        self.assertTrue(self.store().get_checklist()["location"])
        self.assertEqual(CustomerAddress.objects.count(), 1)

    def test_failed_correction_retries_without_duplicate_questions_or_writes(self):
        self.send("add_delivery_address", "address", ADDRESS)
        reply = self.send("update_delivery_address", "pincode 123", {"postal_code": "123"})
        self.assertEqual(reply.count("valid 6-digit pincode"), 1)
        self.assertFalse(self.store().get_checklist()["location"])
        self.send("confirm_delivery_address", "yes")
        self.assertFalse(self.store().get_checklist()["location"])
        self.assertEqual(CustomerAddress.objects.get().components["postal_code"], "110001")
        reply = self.send("update_delivery_address", "Flat 5, 110001", {"house_or_flat": "Flat 5", "postal_code": "110001"})
        self.assertEqual(reply.count("Please confirm"), 1)
        self.assertFalse(self.store().get_checklist()["location"])
        self.send("confirm_delivery_address", "yes")
        self.assertTrue(self.store().get_checklist()["location"])
        self.assertEqual(CustomerAddress.objects.count(), 1)
        self.assertIsNone(CustomerAddress.objects.get().location_coordinates)

    def test_no_saved_addresses_can_transition_to_add_and_then_confirm(self):
        # Choosing an address is a typed selection. With none saved, the
        # classifier starts the add flow instead of selecting an entry.
        self.send("add_delivery_address", "full address", ADDRESS)
        self.assertEqual(self.pending().sub_intent, "confirm_delivery_address")
        self.send("confirm_delivery_address", "yes")
        self.assertTrue(self.store().get_checklist()["location"])

    def test_listing_selection_and_confirmation_use_selected_address(self):
        first = self.saved()
        work = self.saved(label="Work", default=False, components={**ADDRESS, "house_or_flat": "Flat 8"})
        self.send("choose_delivery_address", "choose my address")
        reply = self.send("confirm_delivery_address", "Work")
        self.assertIn("Flat 8", reply)
        self.assertEqual(self.store().get_delivery_address()["address_id"], str(work.id))
        self.send("confirm_delivery_address", "yes")
        first.refresh_from_db()
        work.refresh_from_db()
        self.assertFalse(first.is_default)
        self.assertTrue(work.is_default)
        self.assertTrue(self.store().get_checklist()["location"])

    def test_coverage_question_finishes_using_followup_pincode(self):
        self.send("verify_address_for_delivery", "do you deliver?")
        self.assertEqual(self.pending().sub_intent, "verify_address_for_delivery")
        reply = self.send("add_delivery_address", "110001", {"postal_code": "110001"})
        self.assertEqual(reply, "Yes, we deliver to 110001!")
        self.assertEqual(self.store().get_ongoing_queries(), ([], None))
        self.assertEqual(CustomerAddress.objects.count(), 0)
