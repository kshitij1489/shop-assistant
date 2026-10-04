"""Task and slot ownership across persisted conversation turns."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.llm.schemas import ActionProposal, ClassifiedMessages, IntentClassification
from chatbot_core.logic.cafe.workflow import graph, runner
from orders.models import CheckoutSettings, CustomerAddress, MenuItem, MenuItemVariant, Order
from tests.support.checkout import CheckoutFixture


class TaskMatchingTests(CheckoutFixture, TestCase):
    def setUp(self):
        super().setUp()
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.large = MenuItemVariant.objects.create(menu_item=self.item, size='Large',
                                                    aliases=['grande'], price='150')
        self.store = self.graph_store()
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))

    def addition(self, variant=None):
        return ActionProposal(kind='CHANGE_BASKET', basket={
            'lines': [{'action': 'add', 'item_id': str(self.item.pk), 'variant_id': variant,
                       'quantity': 2, 'modifiers': [], 'target_number': None, 'unresolved': []}],
            'unresolved': [], 'catalog_miss': False})

    def row(self, action=None, *, reply_to=None, route=('placing_order', 'add_to_basket')):
        return IntentClassification(query='Use the large size for two coffees', intent=route[0],
            sub_intent=route[1], action=action, reply_to=reply_to, clarification=None)

    def send(self, text, *rows):
        with patch.object(graph, 'normalize_and_classify', return_value=ClassifiedMessages(
                classifications=list(rows), declared_constraints=[])):
            reply, _ = runner.run_conversation(self.tenant, self.store, text, self.customer)
        self.store = type(self.store)('chat', tenant_id=self.tenant.pk, platform='website')
        return reply

    def pending_size(self):
        self.assertIn('size', self.send('Add two coffees', self.row(self.addition())))
        return self.store.get_ongoing_queries()[0][-1]

    def assert_pending_unchanged(self, pending):
        saved = next(p for p in self.store.get_ongoing_queries()[0] if p.query_id == pending.query_id)
        self.assertEqual(saved.to_dict(), pending.to_dict())

    def test_labelled_name_overrides_actionless_or_split_misclassification(self):
        for split in (False, True):
            with self.subTest(split=split):
                self.store.set_ongoing_queries([], None)
                pending = self.pending_size()
                rows = [self.row(reply_to=str(pending.query_id), route=('general', 'greeting'))]
                if split:
                    rows.append(self.row(self.addition(str(self.large.pk)), reply_to=str(pending.query_id)))
                self.send('name: Large Guest', *rows)
                self.assertEqual(self.store.get_basket().items[0]['quantity'], 1)
                self.assert_pending_unchanged(pending)
                self.session.refresh_from_db()
                self.assertEqual(self.session.state['checkout']['fields']['name'], 'Large Guest')

    def test_unrelated_name_cannot_repair_missing_size_or_exhaust_task(self):
        pending = self.pending_size()
        for _ in range(3):
            reply = self.send('Alice', self.row(self.addition(str(self.large.pk)),
                                                reply_to=str(pending.query_id)))
            self.assertIn('size', reply)
            self.assert_pending_unchanged(pending)
            self.assertEqual(self.store.get_basket().items[0]['quantity'], 1)
        self.send('grande', self.row(self.addition(str(self.large.pk)), reply_to=str(pending.query_id)))
        self.assertEqual([(p['size'], p['quantity']) for p in self.store.get_basket().items],
                         [('Regular', 1), ('Large', 2)])
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_removal_cannot_consume_pending_addition(self):
        pending = self.pending_size()
        remove = ActionProposal(kind='CHANGE_BASKET', basket={
            'lines': [{'action': 'remove', 'item_id': None, 'variant_id': None,
                       'quantity': None, 'modifiers': None, 'target_number': None,
                       'reference': {'by': 'id', 'value': '1'}, 'unresolved': []}],
            'unresolved': [], 'catalog_miss': False})
        self.send('Remove entry 1', self.row(remove, reply_to=str(pending.query_id)))
        self.assertTrue(self.store.get_basket().is_empty())
        self.assert_pending_unchanged(pending)

    def test_independent_product_add_cannot_consume_pending_addition(self):
        pending = self.pending_size()
        item = MenuItem.objects.create(tenant=self.tenant, name='Brownie')
        variant = MenuItemVariant.objects.create(menu_item=item, size='Standard', price='80')
        action = self.addition(str(variant.pk))
        action.basket.lines[0].item_id = str(item.pk)
        action.basket.lines[0].quantity = 1
        reply = self.send('Also add one Standard Brownie. I will choose the coffee size later.',
                          self.row(action, reply_to=str(pending.query_id)))
        self.assertIn('Added 1', reply)
        self.assert_pending_unchanged(pending)
        self.send('grande', self.row(self.addition(str(self.large.pk)), reply_to=str(pending.query_id)))
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual([(p['name'], p['size'], p['quantity']) for p in self.store.get_basket().items],
                         [('Coffee', 'Regular', 1), ('Brownie', 'Standard', 1), ('Coffee', 'Large', 2)])

    def test_unresolved_product_choice_can_still_complete_pending_addition(self):
        action = self.addition()
        action.basket.lines[0].item_id = None
        action.basket.unresolved = ['Which item would you like?']
        self.send('Add two, I will choose the item next', self.row(action))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.send('Two large coffees', self.row(self.addition(str(self.large.pk)), reply_to=str(pending.query_id)))
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual(self.store.get_basket().items[-1]['quantity'], 2)

    def pending_address(self):
        text = '42 Main Street, Delhi, India'
        row = IntentClassification(query=text, intent='location_based', sub_intent='add_delivery_address',
                                   reply_to=None, clarification='Please provide pincode.', action=None)
        with patch('chatbot_core.logic.cafe.intent_handler.location_based.extract_address_with_gpt',
                   return_value={'street_address': '42 Main Street', 'city': 'Delhi',
                                 'state': 'Delhi', 'country': 'India'}):
            self.send(text, row)

    def supply_postcode(self, value):
        with patch('chatbot_core.logic.cafe.intent_handler.location_based.extract_address_with_gpt') as extract, \
                patch('chatbot_core.logic.cafe.intent_handler.location_based.verify_delivery_pincode', return_value=True):
            reply = self.send(f'postal code: {value}', self.row(ActionProposal(
                kind='SET_CHECKOUT_FIELD', field='postal_code', value=value)))
        extract.assert_not_called()
        return reply

    def test_labelled_postcode_completes_separate_address_task(self):
        self.pending_address()
        reply = self.supply_postcode('110001')
        self.assertIn('Please confirm', reply)
        self.assertEqual(self.store.get_delivery_address()['postal_code'], '110001')
        self.assertEqual(self.store.get_delivery_address()['street_address'], '42 Main Street')
        self.assertFalse(self.store.get_checklist().get('location'))
        self.assertEqual([p.sub_intent for p in self.store.get_ongoing_queries()[0]], ['confirm_delivery_address'])
        self.assertEqual(CustomerAddress.objects.get(customer=self.customer).components['postal_code'], '110001')
        self.session.refresh_from_db()
        self.assertFalse((self.session.state or {}).get('checkout'))

    def test_invalid_labelled_postcode_keeps_address_task_open(self):
        self.pending_address()
        reply = self.supply_postcode('11000')
        self.assertIn('6-digit pincode', reply)
        self.assertFalse(CustomerAddress.objects.filter(customer=self.customer).exists())
        self.assertEqual(self.store.get_delivery_address()['street_address'], '42 Main Street')
        self.assertEqual([p.sub_intent for p in self.store.get_ongoing_queries()[0]], ['add_delivery_address'])

    def test_address_task_postcode_also_updates_existing_checkout(self):
        self.send('checkout')
        self.send('delivery')
        self.pending_address()
        reply = self.supply_postcode('110001')
        self.assertIn('Please confirm', reply)
        self.session.refresh_from_db()
        draft = self.session.state['checkout']
        self.assertEqual(draft['fields']['postal_code'], '110001')
        self.assertIn('42 Main Street', draft['fields']['address'])
        self.assertIsNone(draft.get('quote'))
        self.assertFalse(Order.objects.exists())

    def test_cart_command_and_information_detour_preserve_size_task(self):
        pending = self.pending_size()
        reply = self.send('show basket', self.row(self.addition(str(self.large.pk)),
                                                 reply_to=str(pending.query_id)))
        self.assertIn('Your basket', reply)
        self.assert_pending_unchanged(pending)
        with patch('chatbot_core.logic.cafe.intent_handler.placing_order.generate_response_from_knowledge',
                   return_value='Pickup is available.'):
            self.send('How does pickup work?', self.row(reply_to=str(pending.query_id),
                      route=('placing_order', 'order_channels_and_modes')))
        self.assert_pending_unchanged(pending)
        self.send('Large', self.row(self.addition(str(self.large.pk)), reply_to=str(pending.query_id)))
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_polite_restart_clears_unfinished_draft_and_pending_size(self):
        self.pending_size()
        self.graph_turn(self.store, 'name: Alice')
        self.graph_turn(self.store, 'delivery')
        reply = self.send('new order please', self.row(ActionProposal(kind='CONTINUE_CHECKOUT')))
        self.assertIn('Started a new order', reply)
        self.assertTrue(self.store.get_basket().is_empty())
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertFalse(self.store.get_checklist().get('checkout'))
        self.session.refresh_from_db()
        self.assertFalse(self.session.state['checkout'])
        self.assertFalse(Order.objects.exists())

    def test_new_order_status_question_does_not_reset_draft(self):
        self.graph_turn(self.store, 'checkout')
        self.graph_turn(self.store, 'delivery')
        before = deepcopy(self.store.get_basket().to_dict())
        self.send('new order status?', self.row(ActionProposal(kind='SHOW_CART')))
        self.assertEqual(self.store.get_basket().to_dict(), before)
        self.session.refresh_from_db()
        self.assertEqual(self.session.state['checkout']['mode'], 'delivery')

    def test_multiple_labelled_fields_keep_their_independent_values(self):
        name = self.row(ActionProposal(kind='SET_CHECKOUT_FIELD', field='name', value='Alice'))
        phone = self.row(ActionProposal(kind='SET_CHECKOUT_FIELD', field='phone', value='1234567890'))
        self.send('name: Alice; phone: 1234567890', name, phone)
        self.session.refresh_from_db()
        self.assertEqual(self.session.state['checkout']['fields'], {'name': 'Alice', 'phone': '1234567890'})

    def test_explicit_commands_still_obey_capability_checks(self):
        from chatbot_core.runtime_configuration import RuntimeConfiguration
        before = deepcopy(self.store.get_basket().to_dict())
        allows = RuntimeConfiguration.allows
        with patch.object(RuntimeConfiguration, 'allows', lambda config, intent, topic:
                          topic != 'order_confirmation' and allows(config, intent, topic)):
            reply = self.send('name: Alice', self.row(self.addition(str(self.large.pk))))
        self.assertIn('unavailable', reply)
        self.session.refresh_from_db()
        self.assertFalse((self.session.state or {}).get('checkout'))
        self.assertEqual(self.store.get_basket().to_dict(), before)

    def test_size_then_quantity_uses_latest_saved_selection(self):
        addition = self.addition()
        addition.basket.lines[0].quantity = None
        addition.basket.unresolved = ['Which size would you like?']
        self.send('Coffee, size and quantity to follow', self.row(addition))
        pending = self.store.get_ongoing_queries()[0][-1]
        addition.basket.unresolved = []
        addition.basket.lines[0].variant_id = str(self.large.pk)
        self.assertIn('How many', self.send('Large', self.row(addition, reply_to=str(pending.query_id))))
        addition.basket.lines[0].quantity = 2
        self.send('Two', self.row(addition, reply_to=str(pending.query_id)))
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual(self.store.get_basket().items[-1]['quantity'], 2)
