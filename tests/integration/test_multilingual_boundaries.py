"""Script interpretation; run real resolution, persistence and business handlers."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.llm.schemas import ActionProposal, ClassifiedMessages, IntentClassification
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler import location_based
from chatbot_core.logic.cafe.workflow import graph, runner
from orders.models import CheckoutSettings, CustomerAddress, MenuItem, MenuItemVariant, Order
from tests.support.checkout import CheckoutFixture


class MultilingualBoundaryTests(CheckoutFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.store = self.graph_store()
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))

    def row(self, query, rewrite, *, action=None, intent='placing_order', topic='add_to_basket',
            reply_to=None, clarification=None):
        return IntentClassification(query=query, rephrased_sentence=rewrite, intent=intent,
            sub_intent=topic, reply_to=reply_to, clarification=clarification, action=action)

    def send(self, text, *rows):
        with patch.object(graph, 'normalize_and_classify', return_value=ClassifiedMessages(
                classifications=list(rows), declared_constraints=[])):
            return runner.run_conversation(self.tenant, self.store, text, self.customer)[0]

    def ice_creams(self):
        self.store.set_basket(Basket())
        for name in ('Pistachio', 'Vanilla', 'Strawberry', 'Rose', 'Mango', 'Coffee',
                     'Chocolate', 'Orange', 'Coconut', 'Banana'):
            item = MenuItem.objects.create(tenant=self.tenant, name=f'{name} Ice Cream')
            variant = MenuItemVariant.objects.create(menu_item=item, size='Standard', price='100')
            if name == 'Pistachio':
                target = (item, variant)
        item, variant = target
        return ActionProposal(kind='CHANGE_BASKET', basket={
            'lines': [{'action': 'add', 'item_id': str(item.pk), 'variant_id': str(variant.pk),
                       'quantity': 2, 'modifiers': [], 'target_number': None, 'unresolved': []}],
            'unresolved': [], 'catalog_miss': False})

    def test_translated_product_phrase_reaches_catalog_check(self):
        action = self.ice_creams()
        self.send('coloca 2 pistache ice cream no pedido, o da carta', self.row(
            'Coloca 2 pistache ice cream', 'Add 2 pistachio ice creams', action=action))
        self.assertEqual([(r['name'], r['quantity']) for r in self.store.get_basket().items],
                         [('Pistachio Ice Cream', 2)])
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_quantity_after_detour_uses_complete_rewrite_and_preserves_original(self):
        action = self.ice_creams()
        action.basket.lines[0].quantity = None
        action.basket.unresolved = ['How many pistachio ice creams?']
        original = 'pista ice cream chahiye, quantity baad mein bataunga, abhi mat daalna'
        self.send(original, self.row(original, 'Add pistachio ice cream once I supply the quantity',
                                     action=action))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.send('show cart', self.row('show cart', 'Show my basket', action=ActionProposal(kind='SHOW_CART')))
        action.basket.lines[0].quantity = 2
        action.basket.unresolved = []
        self.send('do', self.row('do', 'Add 2 pistachio ice creams', action=action,
                                reply_to=str(pending.query_id), topic='customize_confirmation'))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual(self.store.get_history()[-1]['query_obj']['original_query'], 'do')

    def test_translated_partial_product_stays_ambiguous_then_choice_resolves(self):
        action = self.ice_creams()
        reply = self.send('do ice cream', self.row('do ice cream', 'Add 2 ice creams', action=action))
        self.assertIn('Several menu items', reply)
        self.assertTrue(self.store.get_basket().is_empty())
        pending = self.store.get_ongoing_queries()[0][-1]
        self.assertEqual(pending.rephrased_sentence, 'Add 2 ice creams')
        self.send('pista wala', self.row('pista wala', 'Add 2 pistachio ice creams', action=action,
                                       reply_to=str(pending.query_id)))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)

    def test_address_draft_survives_pause_resume_and_confirmation(self):
        self.tenant.meta = {'serviceable_pincodes': ['122002']}
        self.tenant.save()
        original = 'delivery chahiye bas, tower C, cyber hub ke paas, itna hi yaad hai'
        with patch.object(location_based, 'extract_address_with_gpt', return_value={
                'street_address': 'tower C, cyber hub ke paas'}):
            self.send(original, self.row(original, 'Deliver to tower C, cyber hub ke paas; other fields unknown',
                intent='location_based', topic='add_delivery_address'))
        before = deepcopy(self.store.get_delivery_address())
        pending = self.store.get_ongoing_queries()[0][-1]
        with patch.object(location_based, 'extract_address_with_gpt') as extract, \
                patch('chatbot_core.logic.cafe.intent_handler.general.generate_response_from_knowledge',
                      return_value='Take your time.'):
            self.send('ruk jao, pincode dhoondh raha hoon phone mein', self.row(
                'ruk jao', 'Wait while I find the delivery pincode', intent='general', topic='wait'))
        extract.assert_not_called()
        self.assertEqual(self.store.get_delivery_address(), before)
        self.assertEqual(self.store.get_ongoing_queries()[0][-1].query_id, pending.query_id)
        self.assertFalse(CustomerAddress.objects.exists())
        details = 'mil gaya, 22 DLF phase 2, gurugram, 122002 State: Haryana. Country: India.'
        rewrite = 'Complete the delivery address with 22 DLF phase 2, gurugram, Haryana, India, 122002'
        components = {'street_address': 'tower C, cyber hub ke paas, 22 DLF phase 2',
                      'city': 'gurugram', 'state': 'Haryana', 'country': 'India', 'postal_code': '122002'}
        with patch.object(location_based, 'extract_address_with_gpt', return_value=components) as extract:
            self.send(details, self.row(details, rewrite, intent='location_based', topic='add_delivery_address',
                                       reply_to=str(pending.query_id)))
        self.assertEqual(extract.call_args.kwargs['rephrased_sentence'], rewrite)
        self.assertEqual(extract.call_args.kwargs['original_text'], details)
        self.assertEqual(CustomerAddress.objects.get().components, components)
        self.assertFalse(self.store.get_checklist()['location'])
        pending = self.store.get_ongoing_queries()[0][-1]
        with patch.object(location_based, 'extract_address_with_gpt', return_value={}):
            self.send('haan yehi address theek hai, save kar de', self.row('haan',
                'Confirm the current delivery address', intent='location_based', topic='confirm_delivery_address',
                reply_to=str(pending.query_id)))
        self.assertTrue(self.store.get_checklist()['location'])
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertFalse(Order.objects.exists())

    def test_fulfillment_preference_does_not_start_checkout_and_is_reused(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    self.store.set_basket(Basket())
                self.send('заберу сам', self.row('заберу сам', 'I will collect it myself',
                    topic='order_channels_and_modes', action=ActionProposal(kind='SET_FULFILLMENT', value='pickup')))
                self.assertEqual(self.store.get_checklist()['fulfillment_preference'], 'pickup')
                self.assertFalse(self.store.get_checklist().get('checkout'))
                self.assertFalse(self.store.get_ongoing_queries()[0])
                self.session.refresh_from_db()
                self.assertFalse((self.session.state or {}).get('checkout'))
        self.store.set_basket(deepcopy(self.basket))
        self.send('checkout', self.row('checkout', 'Review my order', topic='order_confirmation',
                                      action=ActionProposal(kind='CONTINUE_CHECKOUT')))
        self.assertEqual(self.store.get_checklist()['checkout']['mode'], 'pickup')
        self.assertTrue(self.store.get_checklist()['checkout']['quote'])
        self.assertFalse(Order.objects.exists())

    def test_mode_change_during_checkout_still_updates_draft(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.graph_turn(self.store, 'checkout')
        self.graph_turn(self.store, 'pickup')
        self.assertTrue(self.store.get_checklist()['checkout']['quote'])
        self.graph_turn(self.store, 'delivery')
        self.assertEqual(self.store.get_checklist()['checkout']['mode'], 'delivery')
        self.assertEqual(self.store.get_checklist()['checkout']['awaiting'], 'address')
        self.assertIsNone(self.store.get_checklist()['checkout']['quote'])

    def test_unavailable_mode_does_not_replace_preference(self):
        del self.config['modes']['dine_in']
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.graph_turn(self.store, 'pickup')
        reply, _ = self.graph_turn(self.store, 'dine_in')
        self.assertIn('not available', reply)
        self.assertEqual(self.store.get_checklist()['fulfillment_preference'], 'pickup')
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_checkout_provisioning_includes_mode_action_route(self):
        from chatbot_core.logic.cafe.workflow.actions import execution_route_closure
        checkout = ('placing_order', 'order_confirmation')
        modes = ('placing_order', 'order_channels_and_modes')
        self.assertIn(modes, execution_route_closure({checkout}))
        self.assertIn(checkout, execution_route_closure({modes}))
