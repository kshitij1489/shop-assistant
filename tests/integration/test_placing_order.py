from tests.support.runtime import classification_result
"""Ordering regressions: real parser, basket, ORM and graph; no provider I/O."""
import importlib
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.logic.cafe import item_parser
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler import base, placing_order as placing
from orders.models import ChatSession, Customer, MenuItem, MenuItemVariant, Order


from tests.support.ordering import OrderingFixture


class PlacingOrderTests(OrderingFixture, TestCase):
    def test_saved_order_changes_redirect_before_parsing_or_mutation(self):
        self.run_intent(self.intent())
        order = Order.objects.create(tenant=self.tenant, customer=self.customer, source='inhouse', total_amount=200)
        self.checklist.update(order=True, order_id=str(order.pk))
        before = deepcopy(self.basket.to_dict())
        saved_order = Order.objects.values().get(pk=order.pk)
        for sub, query in (('update_order', 'Make that three'), ('delete_entry', 'Remove Vanilla')):
            with self.subTest(sub=sub):
                obj = self.intent(sub, query)
                response = self.run_intent(obj)
                self.assertIn('call the store as soon as possible', response)
                self.assertIn('staff will check', response)
                self.assertTrue(obj.is_complete)
                self.assertEqual(obj.follow_up_question, [])
        self.assertEqual(self.basket.to_dict(), before)
        self.assertEqual(Order.objects.values().get(pk=order.pk), saved_order)
        self.payment.assert_not_called()


    def test_store_only_requests_finish_without_mutating_basket_or_checkout(self):
        self.run_intent(self.intent())
        self.checklist['checkout'] = {'mode': 'pickup', 'fields': {'name': 'QA Guest'}}
        before = deepcopy((self.basket.to_dict(), self.checklist))
        for topic, reply in placing.PlacingOrderIntent.STORE_CONTACT_REPLIES.items():
            for placed in (False, True):
                with self.subTest(topic=topic, placed=placed):
                    self.checklist['order'] = placed
                    obj = self.intent(topic, 'Please arrange this')
                    obj.basket_item = {'checkout': True}
                    expected = deepcopy(self.checklist)
                    self.assertEqual(self.run_intent(obj), reply)
                    self.assertTrue(obj.is_complete)
                    self.assertFalse(obj.follow_up_question)
                    self.assertEqual(self.checklist, expected)
                    self.assertEqual(self.basket.to_dict(), before[0])
        self.payment.assert_not_called()

    def test_add_preserves_and_corrects_fields_across_roundtrips(self):
        obj = self.intent(query="2 Vanilla")
        self.assertIn("size", self.run_intent(obj))
        obj, reply = self.follow(obj, "family")
        self.assertTrue(obj.is_complete)
        self.assertIn("Added 2", reply)
        self.assertEqual(self.basket.items[0]["quantity"], 2)
        obj = self.intent(query="0 Vanilla mini tub")
        self.assertIn("positive whole", self.run_intent(obj))
        obj, reply = self.follow(obj, "3")
        self.assertIn("Added 3", reply)

    def test_parser_preserves_original_message_and_pending_context(self):
        with patch('chatbot_core.logic.cafe.item_parser.interpret_order', return_value={}) as extract:
            item_parser.parse_order_text(self.tenant.api_key, 'Set Vanilla quantity to 2',
                                        original_text='make that two', pending={'original_request':'Vanilla'})
        self.assertEqual(extract.call_args.kwargs['original_text'], 'make that two')
        self.assertEqual(extract.call_args.args[3], {'original_request':'Vanilla'})

    def test_invalid_quantities_never_mutate(self):
        for qty in (0, -2, 1.5, True, 'bad', '0', '1.5', '-2'):
            proposal = self.proposal(self.tenant.api_key, '2 Vanilla mini tub')
            proposal['proposal']['lines'][0]['quantity'] = qty
            obj = self.intent()
            self.run_intent(obj, proposal=proposal)
            self.assertFalse(obj.is_complete)
            self.assertTrue(self.basket.is_empty())

    def test_unknown_item_bad_variant_and_mutation_failure(self):
        for field in ('item_id', 'variant_id'):
            proposal = self.proposal(self.tenant.api_key, '2 Vanilla mini tub')
            proposal['proposal']['lines'][0][field] = 'missing'
            obj = self.intent()
            self.run_intent(obj, proposal=proposal)
            self.assertFalse(obj.is_complete)
            self.assertTrue(self.basket.is_empty())
        with patch('chatbot_core.logic.cafe.order_changes.enforce_change', side_effect=ValueError('Limit reached')):
            self.assertIn('Limit reached', self.run_intent(self.intent()))
        self.assertTrue(self.basket.is_empty())

    def test_single_variant_defaults_and_no_false_multi_item_success(self):
        self.assertIn("Added 1", self.run_intent(self.intent(query="Brownie")))
        obj = self.intent(query="Vanilla and Brownie")
        self.assertIn("one menu item", self.run_intent(obj))
        self.assertEqual(len(self.basket.items), 1)

    def test_update_quantity_does_not_require_keyword_and_preserves_size(self):
        self.run_intent(self.intent())
        self.assertIn("Updated", self.run_intent(self.intent("update_order", "make Vanilla 3")))
        self.assertEqual(self.basket.items[0]["quantity"], 3)
        self.assertIn("Updated", self.run_intent(self.intent("update_order", "Vanilla family")))
        self.assertEqual(self.basket.items[0]["quantity"], 3)

    def test_update_missing_quantity_survives_followup(self):
        self.run_intent(self.intent())
        obj = self.intent("update_order", "change Vanilla quantity")
        self.assertIn("new quantity", self.run_intent(obj))
        obj, reply = self.follow(obj, "4")
        self.assertIn("Updated", reply)
        self.assertEqual(self.basket.items[0]["quantity"], 4)

    def test_ambiguous_update_and_delete_require_row_selection(self):
        self.run_intent(self.intent())
        self.run_intent(self.intent(query="Vanilla family"))
        obj = self.intent("update_order", "Vanilla quantity 4")
        self.assertIn("Which entry", self.run_intent(obj))
        obj, reply = self.follow(obj, "2")
        self.assertIn("Updated", reply)
        self.assertEqual([x["quantity"] for x in self.basket.items], [2, 4])
        obj = self.intent("delete_entry", "remove Vanilla")
        self.assertIn("Which entry", self.run_intent(obj))
        obj, reply = self.follow(obj, "1")
        self.assertIn("Removed", reply)
        self.assertEqual(self.basket.items[0]["size"], "family")

    def test_delete_empty_and_unknown_and_cancel(self):
        from chatbot_core.logic.action_resolver import TerminalRejection
        with self.assertRaisesRegex(TerminalRejection, 'basket is empty'):
            self.run_intent(self.intent("delete_entry", "remove Vanilla"))
        self.run_intent(self.intent())
        with self.assertRaisesRegex(TerminalRejection, 'No matching entries'):
            self.run_intent(self.intent("delete_entry", "remove Brownie"))
        before = deepcopy(self.basket.to_dict())
        obj = self.intent("cancel_and_abort", "stop")
        self.run_intent(obj)
        self.assertEqual(self.basket.to_dict(), before)
        self.assertNotIn("stopped", obj.response)

    def test_update_invalid_quantity_and_failure_do_not_claim_success(self):
        self.run_intent(self.intent())
        before = deepcopy(self.basket.items)
        self.assertIn('positive whole', self.run_intent(self.intent('update_order', 'Vanilla quantity -2')))
        with patch('chatbot_core.logic.cafe.order_changes.enforce_change', side_effect=ValueError('Limit reached')):
            self.assertIn('Limit reached', self.run_intent(self.intent('update_order', 'Vanilla quantity 3')))
        self.assertEqual(self.basket.items, before)

    def test_unknown_empty_and_insufficient_inputs(self):
        for sub in (None, [], "unknown"):
            obj = self.intent(sub)
            self.assertTrue(self.run_intent(obj))
            self.assertTrue(obj.is_complete)
        for text in (None, "", " "):
            obj = self.intent(query=text)
            self.assertTrue(self.run_intent(obj))
            self.assertFalse(obj.is_complete)
        self.assertTrue(self.run_intent(self.intent("insufficient_information_order", "huh")))

    def test_checkout_empty_missing_customer_and_incomplete_basket(self):
        self.assertIn("empty", self.run_intent(self.intent("order_confirmation", "checkout")))
        self.run_intent(self.intent())
        customer, self.customer = self.customer, None
        self.assertIn("customer details", self.run_intent(self.intent("order_confirmation", "checkout")))
        self.customer = customer
        self.basket.items[0]["quantity"] = 0
        self.assertIn("invalid entry", self.run_intent(self.intent("order_confirmation", "checkout")))
        self.assertEqual(Order.objects.count(), 0)
        self.payment.assert_not_called()

    def test_checkout_handoff_and_recovery_reuse_exact_order(self):
        self.run_intent(self.intent())
        obj = self.intent("order_confirmation", "checkout")
        self.assertEqual(self.run_intent(obj), "")
        self.assertEqual(obj.handoff_to, "location_based")
        saved = self.checklist["order_id"]
        unrelated = Order.objects.create(tenant=self.tenant, customer=self.customer, total_amount=999, source="inhouse")
        self.checklist.clear()  # Simulate a lost session save after DB commit.
        self.address.update(street_address="Main Road", city="Delhi", state="Delhi", country="India", postal_code="110001")
        self.checklist["location"] = True
        reply = self.run_intent(self.intent("order_payment", "pay"))
        self.assertIn("Pay cash at fulfillment", reply)
        self.assertEqual(self.checklist["order_id"], saved)
        self.assertNotEqual(saved, str(unrelated.pk))
        self.assertEqual(Order.objects.count(), 2)
        order = Order.objects.get(pk=saved)
        self.assertEqual(order.location_coordinates, {"address": "Main Road, Delhi, 110001, India", "pincode": "110001"})
        self.payment.assert_not_called()

    def test_checkout_binding_failure_rolls_back_order(self):
        self.run_intent(self.intent())
        with patch.object(placing, "update_chat_session_order", side_effect=ChatSession.DoesNotExist), self.assertLogs(level="ERROR"):
            self.assertIn("couldn’t verify", self.run_intent(self.intent("order_confirmation", "checkout")))
        self.assertEqual(Order.objects.count(), 0)
        self.assertFalse(self.checklist.get("order"))
        self.assertIsNone(self.checklist.get("order_id"))

    def test_invalid_or_foreign_checkout_reference_never_falls_back(self):
        self.run_intent(self.intent())
        for reference in ("bad-id", "00000000-0000-0000-0000-000000000001"):
            self.checklist.update(order=True, order_id=reference, location=True)
            with self.assertLogs(level="ERROR"):
                self.assertIn("couldn’t verify", self.run_intent(self.intent("order_payment", "pay")))
        self.payment.assert_not_called()
        self.assertEqual(Order.objects.count(), 0)

    def test_failed_payment_preserves_order_and_absolute_url(self):
        self.run_intent(self.intent())
        self.address.update(street_address="Main Road", city="Delhi", state="Delhi", country="India", postal_code="110001")
        self.checklist["location"] = True
        self.run_intent(self.intent("order_confirmation", "checkout"))
        Order.objects.update(payment_mode="online")
        self.payment.side_effect = RuntimeError("offline")
        with self.assertLogs(level="ERROR"):
            self.assertIn("order is saved", self.run_intent(self.intent("order_payment", "pay")))
        self.payment.side_effect = None
        self.payment.return_value = {"payment_url": "https://pay.example/abc?a=1&b=2"}
        self.assertIn("https://pay.example/abc?a=1&b=2", self.run_intent(self.intent("order_payment", "pay")))
        self.assertEqual(Order.objects.count(), 1)

    def test_payment_claims_verified_in_db_and_no_checkout_mutation(self):
        self.run_intent(self.intent())
        self.run_intent(self.intent("order_confirmation", "checkout"))
        self.assertIn("haven’t verified", self.run_intent(self.intent("payment_confirmation", "I paid")))
        order = Order.objects.get(pk=self.checklist["order_id"])
        order.payment_status = Order.PaymentStatus.PAID
        order.save()
        self.assertIn("is confirmed", self.run_intent(self.intent("payment_confirmation", "I paid")))
        self.assertTrue(self.checklist["payment"])
        self.assertIn("already confirmed", self.run_intent(self.intent("order_payment", "pay")))
        self.payment.assert_not_called()
        self.assertIn("call the store as soon as possible", self.run_intent(self.intent(query="Brownie")))

    def test_unknown_name_never_deletes_or_updates_the_only_item(self):
        self.run_intent(self.intent())
        before = deepcopy(self.basket.items)
        for sub, text in (("delete_entry", "remove unicorn"), ("update_order", "unicorn quantity 3")):
            self.assertIn("Which item", self.run_intent(self.intent(sub, text)))
        self.assertEqual(before, self.basket.items)
        self.assertIn("Updated", self.run_intent(self.intent("update_order", "make it 4")))

    def test_payment_handoff_yes_and_no(self):
        self.run_intent(self.intent())
        self.address.update(street_address="Main Road", city="Delhi", state="Delhi", country="India", postal_code="110001")
        self.checklist["location"] = True
        obj = self.intent("order_payment", "order payment", follow_up_question=["Ready for payment?"])
        obj, reply = self.follow(obj, "yes")
        self.assertIn("Pay cash at fulfillment", reply)
        obj = self.intent("order_payment", "order payment", follow_up_question=["Ready for payment?"])
        obj, reply = self.follow(obj, "no")
        self.assertIn("stopped", reply)
        self.payment.assert_not_called()

    def test_checkout_unavailable_item_rolls_back(self):
        self.run_intent(self.intent())
        MenuItem.objects.filter(name="Vanilla").update(is_available=False)
        with self.assertLogs(level="ERROR"):
            self.assertIn("couldn’t verify", self.run_intent(self.intent("order_confirmation", "checkout")))
        self.assertFalse(Order.objects.exists())
        self.payment.assert_not_called()

    def test_changed_cart_and_cancelled_order_never_get_payment_link(self):
        self.run_intent(self.intent())
        self.run_intent(self.intent("order_confirmation", "checkout"))
        self.basket.items[0]["quantity"] += 1
        self.assertIn("differs", self.run_intent(self.intent("order_payment", "pay")))
        Order.objects.update(order_status=Order.Status.CANCELLED)
        self.assertIn("cancelled", self.run_intent(self.intent("order_payment", "pay")))
        self.payment.assert_not_called()

    def test_bad_provider_urls_do_not_claim_success(self):
        self.run_intent(self.intent())
        self.address.update(street_address="Main Road", city="Delhi", state="Delhi", country="India", postal_code="110001")
        self.checklist["location"] = True
        self.run_intent(self.intent("order_confirmation", "checkout"))
        Order.objects.update(payment_mode="online")
        for info in (None, {}, {"payment_url": "/payment/simulate/"}, {"payment_url": "//bad.example/pay"}, {"payment_url": "javascript:alert(1)"}):
            self.payment.return_value = info
            with self.assertLogs(level="ERROR"):
                self.assertIn("couldn’t get", self.run_intent(self.intent("order_payment", "pay")))
        self.assertEqual(Order.objects.count(), 1)

    def test_followup_cart_question_preserves_pending_draft(self):
        obj = self.intent(query="2 Vanilla")
        self.run_intent(obj)
        obj, reply = self.follow(obj, "what is in my cart", "check_order_cart")
        self.assertIn("empty", reply)
        self.assertFalse(obj.is_complete)
        self.assertEqual(obj.basket_item["proposal"]["lines"][0]["quantity"], 2)
        obj, reply = self.follow(obj, "family")
        self.assertIn("Added 2", reply)

    def test_deleting_other_item_does_not_cancel_pending_addition(self):
        self.run_intent(self.intent(query='Brownie'))
        pending = self.intent(query='2 Vanilla')
        self.run_intent(pending)
        # Independent removal goes to its own handler; reply_to is null.
        self.assertIn('Removed', self.run_intent(self.intent('delete_entry', 'remove Brownie')))
        self.assertFalse(pending.is_complete)
        self.assertTrue(self.basket.is_empty())
        pending, reply = self.follow(pending, 'family')
        self.assertIn('Added 2', reply)

    def test_decimal_prices_survive_cart_and_session_serialization(self):
        variant = self.menu["Vanilla"]["item_variant_map"]["mini tub"]
        self.menu["Vanilla"]["pricing"][variant] = "100.75"
        MenuItemVariant.objects.filter(pk=variant).update(price="100.75")
        self.run_intent(self.intent())
        basket = Basket.from_dict(deepcopy(self.basket.to_dict()))
        shown = basket.summary(currency="INR", exponent=2)[0]
        self.assertEqual(shown["unit_price_minor"], 10075)
        self.assertEqual(shown["line_total_minor"], 20150)
        self.assertNotIn("price", shown)
        self.assertIn("INR 201.50", self.run_intent(self.intent("check_order_cart", "cart")))

    def test_checkout_rejects_chat_owned_by_another_customer(self):
        other = Customer.objects.create(tenant=self.tenant, phone="456")
        self.chat.customer = other
        self.chat.save()
        self.run_intent(self.intent())
        with self.assertLogs(level="ERROR"):
            self.assertIn("couldn’t verify", self.run_intent(self.intent("order_confirmation", "checkout")))
        self.assertFalse(Order.objects.exists())

    def test_multiple_quantities_require_clarification(self):
        obj = self.intent(query="2 or 3 Vanilla mini tub")
        self.assertIn("one menu item", self.run_intent(obj))
        self.assertTrue(self.basket.is_empty())

    def test_missing_required_postal_fields_requires_address_handoff(self):
        self.run_intent(self.intent())
        self.customer.location_coordinates = None
        self.customer.save()
        self.address.update(city="Delhi")
        self.checklist["location"] = True
        obj = self.intent("order_confirmation", "checkout")
        self.run_intent(obj)
        self.assertEqual(obj.handoff_to, "location_based")
        self.assertFalse(self.checklist["location"])
        self.payment.assert_not_called()

    def test_pending_update_can_be_replaced_by_explicit_deletion(self):
        self.run_intent(self.intent())
        obj = self.intent("update_order", "Vanilla quantity")
        self.run_intent(obj)
        obj, reply = self.follow(obj, "remove Vanilla", "delete_entry")
        self.assertIn("Removed", reply)
        self.assertTrue(self.basket.is_empty())

    def test_checkout_does_not_discard_pending_item(self):
        self.run_intent(self.intent(query="Brownie"))
        obj = self.intent(query="2 Vanilla")
        self.run_intent(obj)
        obj, reply = self.follow(obj, "checkout", "order_confirmation")
        self.assertIn("finish or cancel", reply)
        self.assertFalse(obj.is_complete)
        self.assertEqual(obj.basket_item["proposal"]["lines"][0]["item_id"], self.menu["Vanilla"]["item_id"])
        self.assertFalse(Order.objects.exists())


class CancellationWorkflowTests(OrderingFixture, TestCase):
    engine_name = 'runner'

    def setUp(self):
        super().setUp()
        from chatbot_core.logic.cafe.session import memory
        self.enterContext(patch.object(memory, '_session_data', {}))
        self.engine = importlib.import_module('chatbot_core.logic.cafe.workflow.' + self.engine_name)
        self.operations = (importlib.import_module('chatbot_core.logic.cafe.workflow.graph')
                           if self.engine_name == 'runner' else self.engine)
        self.session = memory.MemorySessionStore('user', tenant_id=self.tenant.pk, platform='telegram')
        self.classify = self.enterContext(patch.object(self.operations, 'normalize_and_classify'))
        self.enterContext(patch.object(self.operations, 'enqueue_string'))
        self.enterContext(patch.object(self.engine, 'enqueue_string'))

    def send(self, text, topic='cancel_and_abort', intent=None):
        intent = intent or ('general' if topic == 'cancel_and_abort' else 'placing_order')
        pending, _ = self.session.get_ongoing_queries()
        target = pending[-1] if pending else None
        reply_to = None
        clarification = None
        if topic == 'cancel_and_abort' and target:
            reply_to = str(target.query_id)
        if text == 'please cancel' and not target:
            intent, topic = 'placing_order', 'delete_entry'
            clarification = 'Which item in your basket would you like to remove?'
        if text == 'cancel the Vanilla' and target:
            intent, topic, reply_to = 'general', 'cancel_and_abort', str(target.query_id)
        if text == 'cancel Vanilla' and target:
            # The unresolved choice is cancellation versus removal, not an
            # instruction to execute a deletion against the pending addition.
            intent, topic = 'general', 'cancel_and_abort'
            clarification = 'Do you want to stop adding this item or remove it from your basket?'
            reply_to = str(target.query_id)
        if text == 'basket item':
            intent, topic, reply_to = 'placing_order', 'delete_entry', None
            clarification = 'Which basket entry would you like to remove?'
        if text in {'item 1', 'Brownie'} and topic == 'delete_entry' and target:
            reply_to = str(target.query_id)
            if text == 'item 1':
                self.extract.side_effect = None
                proposal = self.proposal(self.tenant.api_key, 'remove Vanilla')
                target.basket_item['proposal'] = proposal['proposal']
                proposal['proposal']['lines'][0]['reference'] = {'by': 'id', 'value': '1'}
                self.extract.return_value = proposal
            else:
                self.extract.side_effect = lambda *a, **kw: self.proposal(a[0], 'remove Brownie')
        if self.session.get_checklist().get('order'):
            intent, topic, reply_to, clarification = 'order_enquiry', 'refund_and_cancellation', None, None
        pending, _ = self.session.get_ongoing_queries()
        pending_item = pending[-1].basket_item if pending else None
        question = pending[-1].get_followup_question() if pending else ''
        action = None if clarification else self.turn_action(
            text, intent, topic, pending_item=pending_item, question=question)
        row = (text, intent, topic, reply_to, clarification, action)
        self.classify.return_value = classification_result([row])
        return self.engine.run_conversation(self.tenant, self.session, text, self.customer)[0]

    def fill_basket(self):
        for text in ('Vanilla mini tub', 'Vanilla family', 'Brownie'):
            self.send(text, 'add_to_basket')

    def test_classifier_clarification_accepts_numeric_selection_after_reload(self):
        self.fill_basket()
        for answer, number in [('1', 1), ('#2', 2)]:
            with self.subTest(answer=answer):
                self.classify.return_value = classification_result([
                    ('Remove a basket item', 'placing_order', 'delete_entry', None,
                     'Which basket entry? Reply with its entry number.')])
                self.engine.run_conversation(self.tenant, self.session, 'remove it', self.customer)
                pending, _ = self.session.get_ongoing_queries()
                self.assertNotIn('proposal', pending[-1].basket_item)
                proposal = self.proposal(self.tenant.api_key, 'remove Vanilla')
                proposal['proposal']['lines'][0]['reference'] = {'by': 'id', 'value': str(number)}
                self.extract.side_effect = None
                self.extract.return_value = proposal
                from tests.support.actions import change_action
                self.classify.return_value = classification_result([
                    (f'Remove basket entry {number}', 'placing_order', 'delete_entry',
                     str(pending[-1].query_id), None, change_action(proposal))])
                self.engine.run_conversation(self.tenant, self.session, answer, self.customer)
                self.assertNotIn(number, [line['item_number'] for line in self.session.get_basket().items])
                self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual([line['name'] for line in self.session.get_basket().items], ['Brownie'])

    def test_menu_question_does_not_declare_a_requirement_or_block_removal(self):
        self.fill_basket()
        self.classify.return_value = classification_result([
            ('Do you have vegan brownies?', 'menu_items', 'dietary_preferences', None, None)])
        with patch('chatbot_core.logic.cafe.intent_handler.menu_items.generate_response_from_knowledge',
                   return_value='Please ask staff.'):
            self.engine.run_conversation(self.tenant, self.session, 'Do you have vegan brownies?', self.customer)
        self.assertEqual(self.session.get_checklist()['declared_constraints'], [])
        self.assertIn('Removed Brownie', self.send('remove Brownie', 'delete_entry'))

    def test_declared_requirement_survives_detour_and_is_disclosed_on_addition(self):
        self.fill_basket()
        removal = self.turn_action('remove Brownie', 'placing_order', 'delete_entry')
        self.classify.return_value = classification_result([
            ('remove Brownie', 'placing_order', 'delete_entry', None, None, removal)],
            declared_constraints=['I have a milk allergy.'])
        self.engine.run_conversation(self.tenant, self.session,
                                     'Remove Brownie. I have a milk allergy.', self.customer)
        self.assertEqual(len(self.session.get_basket().items), 2)
        self.send('show basket', 'check_order_cart')
        self.assertEqual(self.session.get_checklist()['declared_constraints'], ['I have a milk allergy.'])
        reply = self.send('Brownie', 'add_to_basket')
        self.assertIn('Added 1 × Brownie', reply)
        self.assertIn('You mentioned: I have a milk allergy.', reply)
        self.assertIn('staff', reply)
        self.assertEqual(len(self.session.get_basket().items), 3)

    def test_cancel_with_three_items_asks_and_remembers_removal_question(self):
        self.fill_basket()
        before = deepcopy(self.session.get_basket().to_dict())
        reply = self.send('please cancel')
        self.assertEqual(reply.strip(), 'Which item in your basket would you like to remove?')
        self.assertEqual(self.session.get_basket().to_dict(), before)
        pending, index = self.session.get_ongoing_queries()
        self.assertEqual(pending[index].sub_intent, 'delete_entry')
        self.assertIn('Removed Brownie', self.send('Brownie', 'delete_entry'))
        self.assertEqual(len(self.session.get_basket().items), 2)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_cancel_size_question_preserves_all_existing_items(self):
        self.fill_basket()
        self.send('2 Vanilla', 'add_to_basket')
        before = deepcopy(self.session.get_basket().to_dict())
        self.assertIn('Stopped the current request', self.send('cancel'))
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_named_cancel_stops_only_unadded_item(self):
        self.send('Brownie', 'add_to_basket')
        self.send('2 Vanilla', 'add_to_basket')
        before = deepcopy(self.session.get_basket().to_dict())
        self.assertIn('Stopped the current request', self.send('cancel the Vanilla', 'delete_entry'))
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_named_cancel_of_existing_and_pending_item_clarifies_before_removal(self):
        self.send('Vanilla mini tub', 'add_to_basket')
        self.send('Vanilla family', 'add_to_basket')
        self.send('2 Vanilla', 'add_to_basket')
        before = deepcopy(self.session.get_basket().to_dict())
        self.assertIn('stop adding this item or remove it', self.send('cancel Vanilla', 'delete_entry'))
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertIn('Which basket entry', self.send('basket item'))
        self.assertEqual(len(self.session.get_ongoing_queries()[0]), 2)
        self.assertIn('Removed', self.send('item 1', 'delete_entry'))
        self.assertEqual(len(self.session.get_basket().items), 1)
        pending, _ = self.session.get_ongoing_queries()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].basket_item['proposal']['lines'][0]['quantity'], 2)
        self.assertTrue(pending[0].get_followup_question())

    def test_named_cancel_clarification_can_stop_addition_and_preserve_basket(self):
        self.send('Vanilla mini tub', 'add_to_basket')
        self.send('2 Vanilla', 'add_to_basket')
        before = deepcopy(self.session.get_basket().to_dict())
        self.send('cancel Vanilla', 'delete_entry')
        self.assertIn('Stopped the current request', self.send('current request'))
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_removing_other_item_preserves_pending_addition(self):
        self.send('Brownie', 'add_to_basket')
        self.send('2 Vanilla', 'add_to_basket')
        self.assertIn('Removed Brownie', self.send('cancel the Brownie', 'delete_entry'))
        self.assertTrue(self.session.get_basket().is_empty())
        self.assertEqual(len(self.session.get_ongoing_queries()[0]), 1)

    def test_polite_cancel_after_order_refers_to_store_without_changing_basket(self):
        self.fill_basket()
        order = Order.objects.create(tenant=self.tenant, customer=self.customer, source='inhouse', total_amount=300)
        self.session.set_checklist({'order': True, 'order_id': str(order.pk)})
        before = deepcopy(self.session.get_basket().to_dict())
        saved_order = Order.objects.values().get(pk=order.pk)
        self.assertIn('call the store', self.send('Could you cancel, please?'))
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(Order.objects.values().get(pk=order.pk), saved_order)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_placed_order_item_removal_retires_stale_pending_addition(self):
        self.send('Vanilla mini tub', 'add_to_basket')
        self.send('2 Vanilla', 'add_to_basket')
        order = Order.objects.create(tenant=self.tenant, customer=self.customer, source='inhouse', total_amount=100)
        self.session.set_checklist({'order': True, 'order_id': str(order.pk)})
        before = deepcopy(self.session.get_basket().to_dict())
        self.assertIn('call the store', self.send('cancel Vanilla', 'delete_entry'))
        self.assertEqual(self.session.get_basket().to_dict(), before)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))


class PlacingOrderGraphTests(OrderingFixture, TestCase):
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
        self.enterContext(patch.object(memory, "_session_data", {}))
        self.session = memory.MemorySessionStore("user", tenant_id=self.tenant.pk, platform="telegram")
        self.enterContext(patch.object(self.graph, "get_intent", return_value=placing.PlacingOrderIntent))
        self.enterContext(patch.object(base, "get_intent", return_value=placing.PlacingOrderIntent))
        self.enterContext(patch.object(self.graph, "enqueue_string"))
        self.enterContext(patch.object(self.runner, "enqueue_string"))
        self.classify = self.enterContext(patch.object(self.graph, "normalize_and_classify"))

    def send(self, text, sub, *, action=None):
        pending, _ = self.session.get_ongoing_queries()
        reply_to = str(pending[-1].query_id) if pending and sub in {'customize_confirmation','delete_entry'} else None
        pending_item = pending[-1].basket_item if pending else None
        question = pending[-1].get_followup_question() if pending else ''
        if action is None:
            action = self.turn_action(text, 'placing_order', sub, pending_item=pending_item, question=question)
        self.classify.return_value = classification_result([(text, "placing_order", sub, reply_to, None, action)])
        return self.runner.run_conversation(self.tenant, self.session, text, self.customer)[0]

    def test_store_only_routes_ignore_proposed_actions_and_clarifications(self):
        from tests.support.actions import change_action
        from chatbot_core.llm.schemas import ActionProposal
        self.send('Vanilla mini tub', 'add_to_basket')
        before = deepcopy(self.session.get_basket().to_dict())
        actions = {
            'special_requests': change_action(self.proposal(self.tenant.api_key, 'make Vanilla 3')),
            'order_scheduling': ActionProposal(kind='SET_CHECKOUT_FIELD', field='scheduled_at',
                                               value='2026-09-30T19:00:00+05:30'),
        }
        for topic, action in actions.items():
            for proposed in (None, action):
                with self.subTest(topic=topic, action=proposed):
                    self.classify.return_value = classification_result([
                        ('Please arrange this', 'placing_order', topic, 'stale-task',
                         'What details?', proposed)])
                    reply = self.runner.run_conversation(self.tenant, self.session,
                                                         'Please arrange this', self.customer)[0]
                    self.assertIn(placing.PlacingOrderIntent.STORE_CONTACT_REPLIES[topic], reply)
                    self.assertEqual(self.session.get_basket().to_dict(), before)
                    self.assertFalse(self.session.get_checklist().get('checkout'))
                    self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_add_clarification_roundtrip_once_and_history_uses_processed_intent(self):
        reply = self.send("2 Vanilla", "add_to_basket")
        self.assertEqual(reply.count("Which size"), 1)
        reply = self.send("family", "customize_confirmation")
        self.assertIn("Added 2", reply)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))
        self.assertEqual(self.session.get_basket().items[0]["quantity"], 2)
        saved = self.session.get_history()[-1]["query_obj"]
        self.assertEqual(saved["response"], reply)
        self.assertTrue(saved["is_complete"])

    def test_ambiguous_delete_survives_session_restore(self):
        self.send("Vanilla family", "add_to_basket")
        self.send("Vanilla mini tub", "add_to_basket")
        reply = self.send("remove Vanilla", "delete_entry")
        self.assertIn("Which entry", reply)
        reply = self.send("2", "customize_confirmation")
        self.assertIn("Removed", reply)
        self.assertEqual(len(self.session.get_basket().items), 1)
        self.assertEqual(self.session.get_ongoing_queries(), ([], None))

    def test_graph_side_question_repeats_prompt_without_mutation(self):
        self.send("2 Vanilla", "add_to_basket")
        response = self.send("what is in my basket", "check_order_cart")
        self.assertIn("empty", response)
        self.assertEqual(response.count("Which size"), 0)
        self.assertTrue(self.session.get_basket().is_empty())
        self.assertIn("Added 2", self.send("family", "customize_confirmation"))

    def test_graph_checkout_address_handoff_then_payment_confirmation(self):
        from chatbot_core.llm.schemas import ActionProposal
        from chatbot_core.logic.cafe.intent_handler.location_based import LocationBasedIntent
        resolver = {"placing_order": placing.PlacingOrderIntent, "location_based": LocationBasedIntent}.__getitem__
        with patch.object(self.graph, "get_intent", side_effect=resolver), patch.object(base, "get_intent", side_effect=resolver):
            self.send("Vanilla family", "add_to_basket")
            response = self.send("checkout", "order_confirmation")
            self.assertIn("delivery address", response)
            pending, awaiting = self.session.get_ongoing_queries()
            self.assertEqual(pending[awaiting].intent_type, "location_based")
            # Address validation has its own tests; exercise its real handoff
            # contract here without calling a geocoder.
            pending[awaiting].request_handoff("placing_order", sub_intent="order_payment",
                main_query="order payment", follow_up_question=["Ready for payment?"])
            payment_intent = pending[awaiting].build_handoff_intent()
            self.session.set_ongoing_queries([payment_intent], 0)
            self.session.set_delivery_address({"street_address": "Main Road", "city": "Delhi", "state": "Delhi", "country": "India", "postal_code": "110001"})
            checklist = self.session.get_checklist()
            checklist["location"] = True
            self.session.set_checklist(checklist)
            response = self.send("yes", "customize_confirmation",
                                 action=ActionProposal(kind='CONTINUE_CHECKOUT'))
            self.assertIn("Pay cash at fulfillment", response)
            self.assertEqual(self.session.get_ongoing_queries(), ([], None))
            self.assertEqual(Order.objects.count(), 1)
