"""Order enquiry contracts with real ORM reads and the production LangGraph."""
from tests.support.runtime import classification_result, classification_rows
import importlib
import json
import uuid
from copy import deepcopy
from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase

from chatbot_core.logic.cafe import db_utils
from chatbot_core.logic.cafe.intent_handler import base
from chatbot_core.logic.cafe.intent_handler.order_enquiry import OrderEnquiryIntent
from chatbot_core.logic.cafe.order_support import SUPPORT_TOPICS, store_call_response
from chatbot_core.models import TenantInfo
from orders.models import Customer, DeliveryPartner, Order, OrderItem


class OrderFixture:
    def setUp(self):
        super().setUp()
        self.tenant = TenantInfo.objects.create(slug="cafe", display_name="Cafe", meta={"support_email": "help@example.com"})
        from tests.support.runtime import enable_legacy_capabilities
        enable_legacy_capabilities(self.tenant)
        self.customer = Customer.objects.create(tenant=self.tenant, name="User", phone="123")
        self.other = Customer.objects.create(tenant=self.tenant, name="Other", phone="456")
        self.old = self.order(order_status=Order.Status.DELIVERED, payment_status=Order.PaymentStatus.PAID,
                              external_order_id="RECEIPT-123")
        self.latest = self.order()
        self.arguments = ({"items": ["latte"]}, {"city": "Delhi"}, {"payment": False}, [], self.tenant.api_key, self.customer)
        self.provider = self.enterContext(patch("chatbot_core.llm.chains.get_chat_model", side_effect=AssertionError("Unexpected provider call")))

    def order(self, **kwargs):
        values = dict(tenant=self.tenant, customer=self.customer, source="inhouse", total_amount="100.00")
        return Order.objects.create(**(values | kwargs))

    def intent(self, sub="order_status_tracking", query="Where is my order?", **kwargs):
        obj = OrderEnquiryIntent(main_query=query, sub_intent=sub, tenant=self.tenant.pk, chat_id="user", **kwargs)
        obj.platform = "telegram"
        return obj

    def run_intent(self, sub="order_status_tracking", query="Where is my order?", *, complete=True, **kwargs):
        obj = self.intent(sub, query, **kwargs)
        response, followup = obj.process_query(*self.arguments)
        self.assertEqual(obj.is_complete, complete)
        self.assertIsNone(followup)
        self.assertEqual(obj.follow_up_question, [] if complete else [response])
        self.assertIsNone(obj.handoff_to)
        return response


class OrderEnquiryTests(OrderFixture, TestCase):
    def test_support_referrals_need_no_order_details_or_lookup(self):
        self.tenant.meta['support_phone'] = '+91 1234567890'
        self.tenant.save(update_fields=['meta'])
        before = list(Order.objects.values())
        with patch.object(db_utils, '_enquiry_order', side_effect=AssertionError('Order lookup forbidden')):
            for sub in SUPPORT_TOPICS:
                for customer in (self.customer, self.other, None):
                    self.arguments = (*self.arguments[:-1], customer)
                    for query in ('I want a refund', 'order ID:', '#ABC and #DEF', 'Change my placed order'):
                        with self.subTest(sub=sub, query=query, customer=customer):
                            response = self.run_intent(sub, query)
                            self.assertIn('call the store as soon as possible at +91 1234567890', response)
                            self.assertIn('staff will check', response)
                            self.assertIn('No action has been taken', response)
        self.assertEqual(before, list(Order.objects.values()))

    def test_contact_lookup_failure_still_gives_completed_referral(self):
        self.arguments = (*self.arguments[:-1], None)
        with patch('chatbot_core.logic.cafe.order_support.TenantInfo.objects.filter', side_effect=DatabaseError):
            self.assertEqual(self.run_intent('refund_and_cancellation', 'Refund please'), store_call_response())

    def test_dispute_helpers_never_lookup_orders_or_mutate_profiles(self):
        before_orders = list(Order.objects.values())
        before_customers = list(Customer.objects.values())
        with patch.object(db_utils, '_enquiry_order', side_effect=AssertionError('No dispute lookup')):
            for handler in (db_utils.missing_or_wrong_items, db_utils.delivery_problems,
                            db_utils.refund_and_cancellation, db_utils.address_change_enquiry):
                for reference in (None, self.old.pk, 'UNKNOWN', '#ABC and #DEF'):
                    with self.subTest(handler=handler.__name__, reference=reference):
                        self.assertEqual(handler(self.tenant, None, reference), store_call_response(self.tenant))
        # Even the legacy helper must not record change_requests or update the
        # profile when the caller's target is a placed order.
        for status in Order.Status.values:
            self.latest.order_status = status
            self.latest.save(update_fields=['order_status'])
            with self.assertNumQueries(0):
                response = db_utils.address_or_contact_update(
                    self.tenant, self.customer, order_id=self.latest.pk,
                    new_address='12 New Road', new_phone='9999999999', apply_globally=True)
            self.assertEqual(response, store_call_response(self.tenant))
        self.latest.order_status = next(row['order_status'] for row in before_orders if row['id'] == self.latest.pk)
        self.latest.save(update_fields=['order_status'])
        self.assertEqual(before_orders, list(Order.objects.values()))
        self.assertEqual(before_customers, list(Customer.objects.values()))

    def test_store_contacts_and_explicit_limits_do_not_require_receipt(self):
        for meta in ({}, {'support_phone': ' +91 1234567890 ', 'support_email': ' help@example.com '},
                     {'support_phone': [], 'support_email': ' '}, None):
            with self.subTest(meta=meta):
                self.tenant.meta = meta
                response = store_call_response(self.tenant)
                self.assertIn('Please call the store', response)
                self.assertIn('I can’t edit or cancel placed orders, issue refunds or replacements', response)
                self.assertIn('arrange callbacks, contact staff, or open complaint tickets', response)
                self.assertIn('No action has been taken', response)
                if isinstance(meta, dict) and isinstance(meta.get('support_phone'), str):
                    self.assertIn('at +91 1234567890.', response)
                    self.assertIn('email help@example.com.', response)
                else:
                    self.assertNotIn(' at ', response)
                    self.assertNotIn('email ', response)

    def test_restored_elliptical_query_retains_selected_order_reference(self):
        pending = self.intent(query="And its delivery?", follow_up_question=["Which issue?"])
        pending.order_reference = str(self.old.pk)
        snapshot = json.loads(json.dumps(pending.to_dict()))
        restored = base.BaseIntent.from_dict(snapshot)
        self.assertEqual(restored.to_dict(), snapshot)
        response, _ = restored.process_followup(
            self.intent("order_status_tracking", "And status for that?"), *self.arguments,
        )
        self.assertIn(str(self.old.pk), response)
        self.assertNotIn(str(self.latest.pk), response)

    def test_older_state_without_order_reference_still_restores(self):
        snapshot = self.intent(query=f"Status of {self.old.pk}").to_dict()
        snapshot.pop("order_reference")
        restored = base.BaseIntent.from_dict(snapshot)
        self.assertIsNone(restored.order_reference)
        response, _ = restored.process_followup(
            self.intent("order_status_tracking", "And its status?"), *self.arguments,
        )
        self.assertIn(str(self.old.pk), response)

    def test_all_categories_complete_without_mutations_or_model_calls(self):
        before = deepcopy(self.arguments[:4])
        customer = Customer.objects.values().get(pk=self.customer.pk)
        orders = list(Order.objects.values())
        for sub in OrderEnquiryIntent.SUB_INTENT_NAMES:
            with self.subTest(sub=sub):
                self.assertTrue(self.run_intent(sub, follow_up_question=["Stale question?"]))
        self.assertEqual(self.arguments[:4], before)
        self.assertEqual(Customer.objects.values().get(pk=self.customer.pk), customer)
        self.assertEqual(list(Order.objects.values()), orders)
        self.provider.assert_not_called()

    def test_no_orders_returns_visible_completed_answer_for_every_category(self):
        self.arguments = (*self.arguments[:-1], self.other)
        for sub in OrderEnquiryIntent.SUB_INTENT_NAMES:
            with self.subTest(sub=sub):
                response = self.run_intent(sub)
                self.assertTrue(response)
                if sub not in SUPPORT_TOPICS | {"general_order_enquiry"}:
                    self.assertIn("couldn’t find any orders", response)

    def test_missing_unsaved_and_foreign_customers_never_read_orders(self):
        foreign = TenantInfo.objects.create(slug="foreign", display_name="Foreign")
        customers = [None, Customer(tenant=self.tenant), Customer(tenant=foreign)]
        for customer in customers:
            # UUID defaults give unsaved models a PK; an empty lookup is still
            # safe, but missing/foreign identities must be rejected entirely.
            if customer is not None and customer.tenant_id == self.tenant.pk:
                customer.pk = None
            self.arguments = (*self.arguments[:-1], customer)
            for sub in OrderEnquiryIntent.SUB_INTENT_NAMES - SUPPORT_TOPICS:
                with self.subTest(sub=sub, customer=customer), self.assertNumQueries(0):
                    self.assertEqual(self.run_intent(sub), OrderEnquiryIntent.CUSTOMER_REQUIRED_RESPONSE)

    def test_unknown_and_empty_inputs_complete_without_io(self):
        for sub in ("unknown", "", None, ["order_status_tracking"]):
            with self.assertLogs(level="WARNING"), self.assertNumQueries(0):
                self.assertEqual(self.run_intent(sub), OrderEnquiryIntent.UNKNOWN_RESPONSE)
        for query in ("", " \n ", None, 12):
            with self.assertNumQueries(0):
                self.assertEqual(self.run_intent(query=query), OrderEnquiryIntent.EMPTY_QUERY_RESPONSE)
        self.assertIn(str(self.latest.pk), self.run_intent(" ORDER_STATUS_TRACKING "))

    def test_all_order_categories_honor_explicit_older_order(self):
        for sub in OrderEnquiryIntent.SUB_INTENT_NAMES - SUPPORT_TOPICS - {"get_order_history", "general_order_enquiry"}:
            with self.subTest(sub=sub):
                response = self.run_intent(sub, f"Order ID: {self.old.pk}")
                self.assertIn(str(self.old.pk), response)
                self.assertNotIn(str(self.latest.pk), response)

    def test_external_and_bare_receipt_ids(self):
        for query in ("order RECEIPT-123", "order id is RECEIPT-123", "#RECEIPT-123", "RECEIPT-123",
                      str(self.old.pk), self.old.pk.hex, f"order no. {self.old.pk}"):
            with self.subTest(query=query):
                self.assertIn(str(self.old.pk), self.run_intent(query=query))

    def test_invalid_foreign_and_missing_ids_never_fall_back_to_latest(self):
        other_order = self.order(customer=self.other)
        foreign = TenantInfo.objects.create(slug="foreign", display_name="Foreign")
        # Deliberately inconsistent imported data: customer belongs to our tenant.
        foreign_order = self.order(tenant=foreign)
        for reference in (uuid.uuid4(), other_order.pk, foreign_order.pk, "bad-id", "12345"):
            for sub in OrderEnquiryIntent.SUB_INTENT_NAMES - SUPPORT_TOPICS - {"get_order_history", "general_order_enquiry"}:
                with self.subTest(reference=reference, sub=sub):
                    response = self.run_intent(sub, f"order id {reference}", complete=sub != 'order_status_tracking')
                    self.assertIn("couldn’t find that order", response)
                    self.assertNotIn(str(self.latest.pk), response)
        self.assertNotIn(str(foreign_order.pk), self.run_intent("get_order_history"))
        self.assertNotIn(str(foreign_order.pk), self.run_intent())

    def test_ambiguous_or_missing_reference_requests_one_id_without_lookup(self):
        for query in (f"Compare {self.old.pk} and {self.latest.pk}", "order ID:", "#ABC and #DEF",
                      "orders 123 and 456", "order ID 123, 456", "order IDs ABC and DEF"):
            with self.subTest(query=query), self.assertNumQueries(0):
                self.assertEqual(self.run_intent(query=query, complete=False), OrderEnquiryIntent.REFERENCE_RESPONSE)

    def test_history_includes_empty_orders_item_snapshot_and_only_five(self):
        for i in range(5):
            order = self.order()
            if i == 4:
                OrderItem.objects.create(order=order, item_name="Latte (large)", quantity=2,
                                         unit_price=50, total_price=100)
        with self.assertNumQueries(2):
            response = self.run_intent("get_order_history")
        self.assertEqual(response.count("Order "), 5)
        self.assertIn("Item details unavailable", response)
        self.assertIn("Latte (large) x2", response)
        self.assertNotIn(str(self.old.pk), response)
        self.assertNotIn(str(self.latest.pk), response)

    def test_status_handles_missing_values_and_tracking(self):
        partner = DeliveryPartner.objects.create(tenant=self.tenant, name="Courier", partner_type="external",
                                                  status="in_transit", tracking_url="https://tracking.example/order?a=1&b=2")
        self.latest.delivery_partner = partner
        self.latest.order_status = ""
        self.latest.payment_status = ""
        self.latest.save()
        response = self.run_intent()
        for value in ("Unknown", "In transit", partner.tracking_url):
            self.assertIn(value, response)
        self.assertEqual(store_call_response(self.tenant), self.run_intent("delivery_problems"))
        partner.tenant = TenantInfo.objects.create(slug="foreign", display_name="Foreign")
        partner.save()
        self.assertNotIn(partner.tracking_url, self.run_intent())

    def test_support_answers_do_not_claim_actions_or_eligibility(self):
        for status in Order.Status.values + ["unknown"]:
            self.latest.order_status = status
            self.latest.save()
            response = self.run_intent("refund_and_cancellation", "Cancel my order")
            self.assertEqual(response, store_call_response(self.tenant))
        with patch.object(db_utils, "address_or_contact_update", side_effect=AssertionError("Unexpected mutation")):
            response = self.run_intent("address_or_contact_update", "Change address to 12 New Road, phone 9999999999")
        self.assertEqual(response, store_call_response(self.tenant))

    def test_database_error_and_blank_result_clear_stale_answer(self):
        for result in (DatabaseError("offline"), "", None, []):
            obj = self.intent(response="Stale", follow_up_question=["Old question?"])
            with patch.object(db_utils, "order_status", side_effect=result if isinstance(result, Exception) else None,
                              return_value=result):
                if isinstance(result, Exception):
                    with self.assertLogs(level="ERROR"):
                        response, followup = obj.process_query(*self.arguments)
                    self.assertEqual(response, obj.FALLBACK_RESPONSE)
                else:
                    response, followup = obj.process_query(*self.arguments)
                self.assertNotEqual(response, "Stale")
                self.assertTrue(obj.is_complete)
                self.assertIsNone(followup)
                self.assertEqual(obj.follow_up_question, [])

    def test_scoped_history_reuses_reference_and_latest_resets_it(self):
        previous = self.intent(query=f"Status of {self.old.pk}")
        previous.process_query(*self.arguments)
        self.arguments[3].append({"query_obj": previous.to_dict()})
        self.assertIn(str(self.old.pk), self.run_intent("order_status_tracking", "And status for it?"))
        self.assertIn(str(self.latest.pk), self.run_intent(query="And my latest order?"))
        for override in ({"tenant": -1}, {"chat_id": "other"}, {"platform": "whatsapp"}, {"intent_type": "placing_order"}):
            self.arguments[3][:] = [{"query_obj": previous.to_dict() | override}]
            self.assertIn(str(self.latest.pk), self.run_intent(query="And its status?"))
        for entry in (None, {}, {"query_obj": None}, {"query_obj": []}):
            self.arguments[3][:] = [entry]
            self.assertTrue(self.run_intent(query="And its status?"))

    def test_restored_followup_updates_state_and_reference(self):
        pending = self.intent(query=f"Status of {self.old.pk}", follow_up_question=["Old question?"])
        with patch.object(base, "get_intent", return_value=OrderEnquiryIntent):
            pending = base.BaseIntent.from_dict(deepcopy(pending.to_dict()))
        incoming = self.intent("order_status_tracking", "And its status?")
        response, followup = pending.process_followup(incoming, *self.arguments)
        self.assertIn(str(self.old.pk), response)
        self.assertEqual(pending.main_query, incoming.main_query)
        self.assertEqual(pending.sub_intent, incoming.sub_intent)
        self.assertEqual(pending.follow_up_reply, [incoming.main_query])
        self.assertEqual(pending.order_reference, str(self.old.pk))
        self.assertTrue(pending.is_complete)
        self.assertIsNone(followup)
        self.assertEqual(pending.follow_up_question, [])

    def test_support_contact_fallback_does_not_promise_handoff(self):
        self.assertIn("help@example.com", db_utils.support_contacts_line(self.tenant))
        for meta in (None, [], {}, {"support_email": " ", "support_phone": []}):
            self.tenant.meta = meta
            self.assertEqual(db_utils.support_contacts_line(self.tenant), "Please contact the café directly for assistance.")

    def test_colliding_internal_and_external_ids_do_not_select_arbitrarily(self):
        self.latest.external_order_id = str(self.old.pk)
        self.latest.save()
        response = self.run_intent(query=f"Order {self.old.pk}", complete=False)
        self.assertIn("couldn’t find that order", response)
        self.assertNotIn(str(self.old.pk), response)
        self.assertNotIn(str(self.latest.pk), response)

    def test_anonymous_helpers_never_expose_unassigned_orders(self):
        self.order(customer=None)
        with self.assertNumQueries(0):
            self.assertIsNone(db_utils.order_status(None))
            self.assertIn("verified customer record", db_utils.get_order_history(None))
            self.assertEqual(store_call_response(self.tenant), db_utils.delivery_problems(self.tenant, None))


class OrderEnquiryGraphTests(OrderFixture, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")
        cls.graph = importlib.import_module("chatbot_core.logic.cafe.workflow.graph")

    def setUp(self):
        super().setUp()
        from chatbot_core.logic.cafe.session import memory
        from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
        resolve = {"order_enquiry": OrderEnquiryIntent, "placing_order": PlacingOrderIntent}.__getitem__
        self.enterContext(patch.object(self.graph, "get_intent", side_effect=resolve))
        self.enterContext(patch.object(base, "get_intent", side_effect=resolve))
        self.enterContext(patch.object(memory, "_session_data", {}))
        self.session = memory.MemorySessionStore("user", tenant_id=self.tenant.pk, platform="telegram")
        self.enterContext(patch.object(self.runner, "get_chat_ongoing_session", return_value=object()))
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.classify = self.enterContext(patch.object(self.graph, "normalize_and_classify"))
        self.reply_to_pending = False

    def send(self, *queries, customer=None):
        self.classify.return_value = classification_result([(query, "order_enquiry", sub, None, None) for query, sub in queries])
        pending, _ = self.session.get_ongoing_queries()
        if pending and self.reply_to_pending and pending[-1].intent_type == 'order_enquiry':
            self.classify.return_value = classification_result([(*row[:3], str(pending[-1].query_id), None) for row in classification_rows(self.classify.return_value)])
        return self.runner.run_conversation(self.tenant, self.session, " and ".join(query for query, sub in queries),
                                            self.customer if customer is None else customer)[0]

    def test_all_categories_emit_answers_without_pending_work(self):
        for sub in OrderEnquiryIntent.SUB_INTENT_NAMES:
            with self.subTest(sub=sub):
                self.assertTrue(self.send(("Order help", sub)))
                self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual(len(self.session.get_history()), 7)

    def test_empty_account_and_failure_do_not_disappear(self):
        self.assertIn("couldn’t find any orders", self.send(("Status?", "order_status_tracking"), customer=self.other))
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        with patch.object(db_utils, "order_status", side_effect=DatabaseError("offline")), self.assertLogs(level="ERROR"):
            self.assertEqual(self.send(("Status?", "order_status_tracking")), OrderEnquiryIntent.FALLBACK_RESPONSE)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_consecutive_and_multiclause_enquiries_preserve_selected_order(self):
        response = self.send((f"Status of {self.old.pk}", "order_status_tracking"), ("And its status?", "order_status_tracking"))
        self.assertNotIn(str(self.latest.pk), response)
        self.assertIn(str(self.old.pk), self.send(("And its status?", "order_status_tracking")))
        self.assertEqual(self.session.get_history()[-1]["query_obj"]["order_reference"], str(self.old.pk))
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_restored_pending_enquiry_records_processed_answer(self):
        pending = self.intent(query=f"Status of {self.old.pk}", follow_up_question=["Which issue?"])
        self.session.set_ongoing_queries([pending], 0)
        self.reply_to_pending = True
        response = self.send(("And its status?", "order_status_tracking"))
        saved = self.session.get_history()[-1]["query_obj"]
        self.assertEqual(saved["response"], response)
        self.assertEqual(saved["sub_intent"], "order_status_tracking")
        self.assertTrue(saved["is_complete"])
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual(store_call_response(self.tenant), self.send(("And a refund?", "refund_and_cancellation")))

    def test_enquiry_side_question_preserves_pending_order_and_business_state(self):
        from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
        pending = PlacingOrderIntent(main_query="Order latte", sub_intent="add_to_basket", tenant=self.tenant.pk,
                                 chat_id="user", query_id=12, follow_up_question=["Which size?"])
        pending.platform = "telegram"
        self.session.set_ongoing_queries([pending], 0)
        self.session.set_delivery_address({"city": "Delhi"})
        before = deepcopy(self.session.get_basket().to_dict())
        self.reply_to_pending = True
        self.assertIn(str(self.latest.pk), self.send(("My previous order status?", "order_status_tracking")))
        saved, index = self.session.get_ongoing_queries()
        self.assertEqual(index, 0)
        self.assertEqual(saved[0].to_dict(), pending.to_dict())
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(self.session.get_delivery_address(), {"city": "Delhi"})


class OrderSupportWorkflowTests(OrderFixture, TestCase):
    """Exercise the conversation graph with scripted classification."""

    def setUp(self):
        super().setUp()
        from chatbot_core.logic.cafe.session import memory
        self.memory = memory
        self.enterContext(patch.object(memory, '_session_data', {}))
        self.graph = importlib.import_module('chatbot_core.logic.cafe.workflow.graph')
        self.runner = importlib.import_module('chatbot_core.logic.cafe.workflow.runner')
        self.enterContext(patch.object(self.runner, 'get_chat_ongoing_session', return_value=object()))
        self.enterContext(patch.object(self.runner, 'enqueue_string'))
        self.enterContext(patch.object(self.graph, 'enqueue_string'))

    def store(self):
        return self.memory.MemorySessionStore('order-support', tenant_id=self.tenant.pk, platform='telegram')

    def turn(self, store, text, topic='order_status_tracking'):
        pending, _ = store.get_ongoing_queries()
        reply_to = str(pending[-1].query_id) if pending else None
        with patch.object(self.graph, 'normalize_and_classify', return_value=classification_result([(text, 'order_enquiry', topic, reply_to, None)])):
            return self.runner.run_conversation(self.tenant, store, text, self.customer)[0]

    def test_reference_question_survives_restore_and_bare_id_completes(self):
        store = self.store()
        self.assertIn('Please include one order ID', self.turn(store, 'order ID:'))
        pending, index = store.get_ongoing_queries()
        self.assertEqual(index, 0)
        self.assertFalse(pending[0].is_complete)
        store.set_ongoing_queries([base.BaseIntent.from_dict(json.loads(json.dumps(pending[0].to_dict())))], 0)
        response = self.turn(store, 'RECEIPT-123')
        self.assertIn(str(self.old.pk), response)
        self.assertEqual(store.get_ongoing_queries(), ([], None))
        self.assertTrue(store.get_history()[-1]['query_obj']['is_complete'])

    def test_refund_completes_without_answering_pending_order_id_question(self):
        store = self.store()
        self.turn(store, 'order ID:')
        self.assertEqual(store_call_response(self.tenant), self.turn(store, 'I want a refund', 'refund_and_cancellation'))
        self.assertEqual(store.get_ongoing_queries(), ([], None))

    def test_dispute_followups_complete_without_lookup_or_business_changes(self):
        store = self.store()
        before = (list(Order.objects.values()), list(Customer.objects.values()),
                  deepcopy(store.get_basket().to_dict()), deepcopy(store.get_delivery_address()))
        with patch.object(db_utils, '_enquiry_order', side_effect=AssertionError('No dispute lookup')):
            for text, topic in (
                ('You sent the wrong dessert.', 'missing_or_wrong_items'),
                ('I asked for Rose Cardamom and got vanilla.', 'missing_or_wrong_items'),
                ('No reference. Can they call me?', 'missing_or_wrong_items'),
                ('Have you emailed the manager and opened a ticket?', 'missing_or_wrong_items'),
                ('The driver is late and not answering.', 'delivery_problems'),
                ('Please have them call Priya, phone ending 2211.', 'address_or_contact_update'),
                ('Cancel my order and refund me today.', 'refund_and_cancellation'),
                ('I do not have the order number.', 'refund_and_cancellation'),
            ):
                with self.subTest(text=text):
                    self.assertEqual(self.turn(store, text, topic), store_call_response(self.tenant))
                    self.assertEqual(store.get_ongoing_queries(), ([], None))
                    self.assertTrue(store.get_history()[-1]['query_obj']['is_complete'])
        self.assertEqual(before, (list(Order.objects.values()), list(Customer.objects.values()),
                                 store.get_basket().to_dict(), store.get_delivery_address()))

    def test_unclear_reference_reply_does_not_silently_select_latest_order(self):
        store = self.store()
        self.turn(store, 'order ID:')
        response = self.turn(store, 'That one')
        self.assertNotIn(str(self.latest.pk), response)
        self.assertEqual(response.strip(), OrderEnquiryIntent.REFERENCE_RESPONSE)
        pending, _ = store.get_ongoing_queries()
        self.assertEqual(len(pending), 1)
        self.assertFalse(pending[0].is_complete)

    def test_completed_order_context_is_sent_separately_from_pending_question(self):
        store = self.store()
        self.turn(store, f'Status of {self.old.pk}')
        with patch.object(self.graph, 'normalize_and_classify', return_value=classification_result([
                ('Change that', 'order_enquiry', 'refund_and_cancellation', None, None)])) as classify:
            response, _ = self.runner.run_conversation(self.tenant, store, 'Change that', self.customer)
        self.assertEqual(response, store_call_response(self.tenant))
        self.assertEqual(classify.call_args.args, ('Change that', '', ''))
        context = classify.call_args.kwargs['conversation_context']
        self.assertEqual(context['last_completed_request']['main_query'], f'Status of {self.old.pk}')
        self.assertEqual(store.get_ongoing_queries(), ([], None))

    def test_support_referral_retains_optional_reference_without_order_lookup(self):
        store = self.store()
        self.turn(store, f'Status of {self.old.pk}')
        with patch.object(db_utils, '_enquiry_order', side_effect=AssertionError('No support lookup')):
            self.assertEqual(self.turn(store, 'I want a refund', 'refund_and_cancellation'),
                             store_call_response(self.tenant))
        self.assertEqual(store.get_ongoing_queries(), ([], None))
        self.assertEqual(store.get_history()[-1]['query_obj']['order_reference'], str(self.old.pk))
        self.assertIn(str(self.old.pk), self.turn(store, 'And its status?'))

    def test_cancel_it_after_completed_status_refers_to_order_without_model(self):
        store = self.store()
        self.turn(store, f'Status of {self.old.pk}')
        before = list(Order.objects.values())
        response = self.turn(store, 'Cancel my placed order', 'refund_and_cancellation')
        self.assertEqual(response, store_call_response(self.tenant))
        self.assertEqual(store.get_ongoing_queries(), ([], None))
        self.assertEqual(before, list(Order.objects.values()))

    def test_placed_order_cancel_retires_stale_basket_task_without_mutation(self):
        from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
        store = self.store()
        store.set_checklist({'order': True, 'order_id': str(self.latest.pk)})
        pending = PlacingOrderIntent(main_query='Add latte', sub_intent='add_to_basket',
            tenant=self.tenant.pk, chat_id=store.user_id, query_id='p99', follow_up_question=['Which size?'])
        pending.platform = 'telegram'
        store.set_ongoing_queries([pending], 0)
        before = list(Order.objects.values())
        basket = deepcopy(store.get_basket().to_dict())
        # Placing the order retires pre-checkout basket tasks. A stale reply ID
        # must not resurrect one or authorize cancellation of the placed order.
        with patch.object(self.graph, 'normalize_and_classify', return_value=classification_result([
                ('Cancel', 'general', 'cancel_and_abort', 'p99', OrderEnquiryIntent.CANCEL_TARGET_RESPONSE)])):
            response, _ = self.runner.run_conversation(self.tenant, store, 'please cancel', self.customer)
        self.assertIn('That request has already finished', response)
        self.assertEqual(store.get_ongoing_queries(), ([], None))
        response = self.turn(store, 'Cancel my placed order', 'refund_and_cancellation')
        self.assertEqual(response, store_call_response(self.tenant))
        self.assertEqual(list(Order.objects.values()), before)
        self.assertEqual(store.get_basket().to_dict(), basket)

    def test_explicit_task_cancel_preserves_other_work(self):
        from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
        store=self.store()
        tasks=[]
        for index in range(2):
            task=PlacingOrderIntent(main_query='Add latte',sub_intent='add_to_basket',tenant=self.tenant.pk,
                chat_id=store.user_id,query_id=str(index),follow_up_question=['Which size?'])
            task.platform='telegram'
            tasks.append(task)
        store.set_ongoing_queries(tasks,1)
        with patch.object(self.graph,'normalize_and_classify',return_value=classification_result([('Stop first request','general','cancel_and_abort','0',None)])):
            response,_=self.runner.run_conversation(self.tenant,store,'stop the first request',self.customer)
        self.assertIn('Stopped',response)
        self.assertEqual([p.query_id for p in store.get_ongoing_queries()[0]],['1'])
