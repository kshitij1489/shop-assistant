"""Script understanding only; exercise the real resolver, handlers and session."""
from copy import deepcopy
from unittest.mock import patch

from django.test import TestCase

from chatbot_core.llm.schemas import ActionProposal, ClassifiedMessages, IntentClassification
from chatbot_core.logic.action_resolver import resolve_action
from chatbot_core.logic.cafe.checkout import advance_checkout
from chatbot_core.logic.cafe.order_changes import apply_proposal
from chatbot_core.logic.cafe.workflow import graph, runner
from orders.models import CheckoutSettings, CustomerAddress, MenuItem, MenuItemVariant, Order
from tests.support.checkout import CheckoutFixture


class ConversationActionTests(CheckoutFixture, TestCase):
    def test_explicit_supersession_cancels_only_selected_request_before_new_add(self):
        self.ambiguous_basket()
        self.run_actions('Change the coffee', self.basket_action(
            reference={'by': 'name', 'value': 'Coffee'}, quantity=3))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('Remove a coffee', self.basket_action(
            'remove', reference={'by': 'name', 'value': 'Coffee'}))
        unrelated = self.store.get_ongoing_queries()[0][-1]
        before = deepcopy(unrelated.to_dict())
        addition = self.basket_action('add', item_id=str(self.item.pk),
            variant_id=str(self.variant.pk), quantity=2, modifiers=[])
        addition.basket.lines[0].reference = None
        result = ClassifiedMessages(declared_constraints=[], classifications=[
            IntentClassification(query='Forget the quantity edit', intent='general',
                sub_intent='cancel_and_abort', reply_to=str(pending.query_id), clarification=None,
                action=ActionProposal(kind='CANCEL_PENDING_ACTION')),
            IntentClassification(query='Add two coffees instead', intent='placing_order',
                sub_intent='add_to_basket', reply_to=None, clarification=None, action=addition)])
        with patch.object(graph, 'normalize_and_classify', return_value=result):
            runner.run_conversation(self.tenant, self.store,
                'Forget the quantity edit and add two coffees instead', self.customer)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)
        self.assertEqual([p.to_dict() for p in self.store.get_ongoing_queries()[0]], [before])
        reply, _ = self.run_actions('Three', self.basket_action(quantity=3),
                                    reply_to=str(pending.query_id))
        self.assertIn('already finished', reply)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)

    def test_empty_basket_edit_is_terminal_and_does_not_block_new_add(self):
        from chatbot_core.logic.cafe.basket import Basket
        self.store.set_basket(Basket())
        self.run_actions('Change my order', self.basket_action(quantity=3))
        self.assertFalse(self.store.get_ongoing_queries()[0])
        addition = self.basket_action('add', item_id=str(self.item.pk),
            variant_id=str(self.variant.pk), quantity=1, modifiers=[])
        addition.basket.lines[0].reference = None
        self.run_actions('Add one coffee', addition)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 1)
        remaining = self.store.get_ongoing_queries()[0]
        self.assertEqual(remaining, [])
        self.interpreter.assert_not_called()

    def test_new_add_clarification_does_not_exhaust_or_replace_pending_edit(self):
        self.ambiguous_basket()
        self.run_actions('Change the coffee', self.basket_action(
            reference={'by': 'name', 'value': 'Coffee'}, quantity=3))
        pending = self.store.get_ongoing_queries()[0][-1]
        before = deepcopy(pending.to_dict())
        addition = self.basket_action('add', item_id=str(self.item.pk), quantity=2)
        addition.basket.lines[0].reference = None
        addition.basket.unresolved = ['Which size for the new coffee?']
        self.run_actions('Add two coffees', addition, reply_to=str(pending.query_id))
        remaining = self.store.get_ongoing_queries()[0]
        self.assertEqual(len(remaining), 2)
        self.assertEqual(remaining[0].to_dict(), before)
        self.assertEqual(remaining[1].sub_intent, 'add_to_basket')
        self.assertEqual(remaining[1].ignored_count, 1)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 1)

    def test_independent_address_clarification_does_not_block_add_in_either_order(self):
        from chatbot_core.logic.cafe.basket import Basket
        for address_first in (False, True):
            with self.subTest(address_first=address_first):
                self.store = self.graph_store()
                self.store.set_basket(Basket())
                self.store.set_ongoing_queries([], None)
                addition = self.basket_action('add', item_id=str(self.item.pk),
                    variant_id=str(self.variant.pk), quantity=2, modifiers=[])
                addition.basket.lines[0].reference = None
                rows = [IntentClassification(query='Add two coffees', intent='placing_order',
                    sub_intent='add_to_basket', reply_to=None, clarification=None, action=addition),
                    IntentClassification(query='Use one of my saved addresses', intent='location_based',
                    sub_intent='choose_delivery_address', reply_to=None,
                    clarification='Which saved address should I use?', action=None)]
                if address_first:
                    rows.reverse()
                with patch.object(graph, 'normalize_and_classify', return_value=ClassifiedMessages(
                        classifications=rows, declared_constraints=[])):
                    reply, _ = runner.run_conversation(self.tenant, self.store,
                        'Add two coffees; I have two saved addresses', self.customer)
                self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)
                self.assertIn('Which saved address', reply)
                self.assertEqual([p.intent_type for p in self.store.get_ongoing_queries()[0]], ['location_based'])
                self.assertFalse(self.store.get_checklist()['location'])
                self.assertFalse(Order.objects.exists())

    def test_deferred_quantity_answer_completes_add_once_after_information_detour(self):
        from chatbot_core.logic.cafe.basket import Basket
        self.store.set_basket(Basket())
        addition = self.basket_action('add', item_id=str(self.item.pk),
            variant_id=str(self.variant.pk), quantity=None, modifiers=[])
        addition.basket.lines[0].reference = None
        addition.basket.unresolved = ['How many coffees?']
        self.run_actions('Coffee please, I will give the quantity later', addition)
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('Show the basket first', ActionProposal(kind='SHOW_CART'))
        self.assertTrue(self.store.get_basket().is_empty())
        self.assertEqual(self.store.get_ongoing_queries()[0][-1].query_id, pending.query_id)
        addition.basket.unresolved = []
        addition.basket.lines[0].quantity = 2
        self.run_actions('Two', addition, reply_to=str(pending.query_id))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)
        self.assertFalse(self.store.get_ongoing_queries()[0])
        reply, _ = self.run_actions('Two', addition, reply_to=str(pending.query_id))
        self.assertIn('already finished', reply)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)

    def test_conditional_add_is_not_executed_as_an_independent_prefix(self):
        addition = self.basket_action('add', item_id=str(self.item.pk),
            variant_id=str(self.variant.pk), quantity=2, modifiers=[])
        addition.basket.lines[0].reference = None
        addition.basket.unresolved = ['Which address must qualify before adding?']
        before = deepcopy(self.store.get_basket().to_dict())
        self.run_actions('Add only if delivery is possible to the address I choose', addition)
        self.assertEqual(self.store.get_basket().to_dict(), before)
        self.assertEqual(self.store.get_ongoing_queries()[0][-1].sub_intent, 'add_to_basket')

    def test_uncertain_quantity_reply_asks_without_losing_deferred_add(self):
        from chatbot_core.logic.cafe.basket import Basket
        self.store.set_basket(Basket())
        addition = self.basket_action('add', item_id=str(self.item.pk),
            variant_id=str(self.variant.pk), quantity=None, modifiers=[])
        addition.basket.lines[0].reference = None
        addition.basket.unresolved = ['How many coffees?']
        self.run_actions('Coffee; quantity later', addition)
        pending = self.store.get_ongoing_queries()[0][-1]
        question = 'Do you mean two coffees or would you like the map link?'
        result = ClassifiedMessages(declared_constraints=[], classifications=[
            IntentClassification(query='do', intent='insufficient_information',
                sub_intent='insufficient_information', reply_to=None,
                clarification=question, action=None)])
        with patch.object(graph, 'normalize_and_classify', return_value=result):
            reply, _ = runner.run_conversation(self.tenant, self.store, 'do', self.customer)
        self.assertEqual(reply, question)
        self.assertTrue(self.store.get_basket().is_empty())
        saved = next(p for p in self.store.get_ongoing_queries()[0] if p.query_id == pending.query_id)
        self.assertEqual(saved.to_dict(), pending.to_dict())
        addition.basket.unresolved = []
        addition.basket.lines[0].quantity = 2
        self.run_actions('Two coffees', addition, reply_to=str(pending.query_id))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)

    def test_clear_schedule_is_distinct_from_setting_text_and_preserves_contact(self):
        self.run_actions('Pickup', ActionProposal(kind='SET_FULFILLMENT', value='pickup'))
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('Tomorrow evening', ActionProposal(kind='SET_CHECKOUT_FIELD',
            field='scheduled_at', value='2030-01-01T19:00:00'), reply_to=str(pending.query_id))
        reply, _ = self.run_actions('Now please', ActionProposal(kind='CLEAR_CHECKOUT_FIELD',
            field='scheduled_at'), reply_to=str(pending.query_id))
        self.assertNotIn('scheduled_at', self.store.get_checklist()['checkout']['fields'])
        self.assertIn('Reply confirm', reply)
        self.assertFalse(Order.objects.exists())
        reply, _ = self.run_actions('Now please', ActionProposal(kind='SET_CHECKOUT_FIELD',
            field='scheduled_at', value='now please'), reply_to=str(pending.query_id))
        self.assertIn('contact the store', reply)
        self.assertNotIn('scheduled_at', self.store.get_checklist()['checkout']['fields'])

    def test_rejected_quantity_closes_request_and_next_add_succeeds(self):
        addition = self.basket_action('add', item_id=str(self.item.pk),
            variant_id=str(self.variant.pk), quantity=10**30, modifiers=[])
        addition.basket.lines[0].reference = None
        before = deepcopy(self.store.get_basket().to_dict())
        reply, _ = self.run_actions('Add a huge amount', addition)
        self.assertIn('above the supported maximum', reply)
        self.assertEqual(self.store.get_basket().to_dict(), before)
        self.assertFalse(self.store.get_ongoing_queries()[0])
        addition.basket.lines[0].quantity = 2
        self.run_actions('Add two instead', addition)
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)

    def test_pending_catalog_choice_adds_without_an_existing_basket_reference(self):
        from chatbot_core.logic.cafe.basket import Basket
        self.store.set_basket(Basket())
        choice = self.basket_action('add', item_id=None, quantity=2, modifiers=[])
        choice.basket.lines[0].reference = None
        choice.basket.unresolved = ['Which drink would you like?']
        self.run_actions('Add two drinks', choice)
        pending = self.store.get_ongoing_queries()[0][-1]
        choice.basket.unresolved = []
        choice.basket.lines[0].item_id = str(self.item.pk)
        choice.basket.lines[0].variant_id = str(self.variant.pk)
        self.run_actions('The coffee', choice, reply_to=str(pending.query_id))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 2)
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_similarly_named_products_are_offered_before_the_named_one_is_added(self):
        from chatbot_core.logic.cafe.basket import Basket
        self.store.set_basket(Basket())
        flavours = {}
        for name in ('Chocolate overload ice cream', 'Just chocolate ice cream', 'Cherry and chocolate ice cream'):
            item = MenuItem.objects.create(tenant=self.tenant, name=name)
            flavours[name] = (item, MenuItemVariant.objects.create(menu_item=item, size='Cup', price='120'))
        guess, cup = flavours['Just chocolate ice cream']
        addition = self.basket_action('add', item_id=str(guess.pk), variant_id=str(cup.pk),
                                      quantity=1, modifiers=[])
        addition.basket.lines[0].reference = None
        reply, _ = self.run_actions('Add a chocolate ice cream', addition)
        self.assertIn('Which item do you mean', reply)
        for name in flavours:
            self.assertIn(name, reply)
        self.assertTrue(self.store.get_basket().is_empty())
        pending = self.store.get_ongoing_queries()[0][-1]
        chosen, cup = flavours['Cherry and chocolate ice cream']
        addition.basket.lines[0].item_id, addition.basket.lines[0].variant_id = str(chosen.pk), str(cup.pk)
        reply, _ = self.run_actions('Cherry and Chocolate ice cream', addition, reply_to=str(pending.query_id))
        self.assertIn('Added 1 × Cherry and chocolate ice cream', reply)
        self.assertEqual([row['item_id'] for row in self.store.get_basket().items], [str(chosen.pk)])
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.interpreter.assert_not_called()

    def test_contextual_yes_places_current_quote_and_repeated_confirmation_reuses_order(self):
        self.run_actions('Pickup', ActionProposal(kind='SET_FULFILLMENT', value='pickup'))
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.assertFalse(Order.objects.exists())
        self.run_actions('yes', ActionProposal(kind='CONFIRM_ORDER'),
                         reply_to=str(pending.query_id))
        order_id = Order.objects.get().pk
        self.run_actions('confirm order', ActionProposal(kind='CONFIRM_ORDER'))
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Order.objects.get().pk, order_id)

    def test_payment_request_does_not_authorize_order_placement(self):
        self.run_actions('Pickup', ActionProposal(kind='SET_FULFILLMENT', value='pickup'))
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('pay', ActionProposal(kind='CONTINUE_CHECKOUT'), reply_to=str(pending.query_id))
        self.assertFalse(Order.objects.exists())

    def test_contextual_yes_cannot_skip_missing_checkout_details(self):
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('yes', ActionProposal(kind='CONFIRM_ORDER'), reply_to=str(pending.query_id))
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.store.get_checklist()['checkout']['awaiting'], 'mode')

    def setUp(self):
        super().setUp()
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.store = self.graph_store()
        self.enterContext(patch.object(graph, 'enqueue_string'))
        self.enterContext(patch.object(runner, 'enqueue_string'))
        self.interpreter = self.enterContext(patch(
            'chatbot_core.logic.cafe.order_interpreter.interpret_order',
            side_effect=AssertionError('Resolved actions must not be interpreted again')))

    def basket_action(self, operation='update', reference=None, **fields):
        return ActionProposal(kind='CHANGE_BASKET', basket={
            'lines': [{'action': operation, 'item_id': None, 'variant_id': None, 'quantity': None,
                       'modifiers': None, 'target_number': None, 'unresolved': [],
                       'reference': reference or {'by': 'focus'}, **fields}],
            'unresolved': [], 'catalog_miss': False})

    def ambiguous_basket(self):
        basket = self.store.get_basket()
        basket.items.append({**deepcopy(basket.items[0]), 'item_number': 9, 'size': 'Large'})
        self.store.set_basket(basket)

    def test_absent_target_with_nonempty_basket_is_terminal_and_checkout_can_continue(self):
        reply, _ = self.run_actions('Remove missing entry', self.basket_action(
            'remove', reference={'by': 'id', 'value': '99'}))
        self.assertIn('No matching entries', reply)
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual(self.store.get_history()[-1]['query_obj']['outcome'], 'terminal_rejection')
        reply, _ = self.run_actions('Checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        self.assertIn('fulfillment', reply)

    def test_pending_target_removed_in_same_turn_cannot_block_checkout(self):
        action = self.basket_action('remove', reference={'by': 'id', 'value': '1'})
        action.basket.unresolved = ['How many should I remove?']
        self.run_actions('Remove some coffee', action)
        self.assertTrue(self.store.get_ongoing_queries()[0])
        reply, _ = self.run_actions('Remove all and checkout',
            self.basket_action('remove', reference={'by': 'id', 'value': '1'}),
            ActionProposal(kind='CONTINUE_CHECKOUT'))
        self.assertIn('empty', reply)
        self.assertNotIn('pending basket change', reply)
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def test_checkout_failure_is_resumable_after_reload_and_information_detour(self):
        from django.db import DatabaseError
        with patch('chatbot_core.logic.cafe.checkout.advance_checkout', side_effect=DatabaseError('offline')):
            self.run_actions('Checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.assertEqual(pending.outcome, 'temporarily_blocked')
        self.store = type(self.store)('chat', tenant_id=self.tenant.pk, platform='website')
        self.run_actions('Show cart', ActionProposal(kind='SHOW_CART'))
        self.assertEqual(self.store.get_ongoing_queries()[0][-1].query_id, pending.query_id)
        reply, _ = self.run_actions('Try again', ActionProposal(kind='CONTINUE_CHECKOUT'),
                                    reply_to=str(pending.query_id))
        self.assertIn('fulfillment', reply)
        self.assertEqual(self.store.get_ongoing_queries()[0][-1].outcome, 'needs_clarification')

    def test_repeated_clarifications_keep_valid_work_resumable(self):
        addition = self.basket_action('add', item_id=str(self.item.pk),
                                    variant_id=str(self.variant.pk), quantity=None, modifiers=[])
        addition.basket.lines[0].reference = None
        addition.basket.unresolved = ['How many coffees?']
        self.run_actions('Add coffee', addition)
        pending = self.store.get_ongoing_queries()[0][-1]
        for _ in range(4):
            self.run_actions('Not sure yet', addition, reply_to=str(pending.query_id))
            self.assertEqual(self.store.get_ongoing_queries()[0][-1].query_id, pending.query_id)
        addition.basket.unresolved = []
        addition.basket.lines[0].quantity = 2
        self.run_actions('Two more', addition, reply_to=str(pending.query_id))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)
        self.assertFalse(self.store.get_ongoing_queries()[0])

    def run_actions(self, text, *actions, reply_to=None):
        from chatbot_core.logic.cafe.workflow.actions import action_route
        result = ClassifiedMessages(declared_constraints=[], classifications=[
            IntentClassification(query=text, intent=action_route(action)[0],
                sub_intent=action_route(action)[1], reply_to=reply_to, clarification=None, action=action)
            for action in actions])
        with patch.object(graph, 'normalize_and_classify', return_value=result):
            return runner.run_conversation(self.tenant, self.store, text, self.customer)

    def test_focus_survives_session_reload_and_later_actions_see_current_state(self):
        add = self.basket_action('add', item_id=str(self.item.pk), variant_id=str(self.variant.pk),
                                 quantity=1, reference=None)
        add.basket.lines[0].reference = None
        self.run_actions('Add a coffee and make it three', add, self.basket_action(quantity=3))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)
        self.assertEqual(self.store.get_checklist()['basket_focus'], 1)
        self.store = type(self.store)('chat', tenant_id=self.tenant.pk, platform='website')
        self.run_actions('Actually make that one', self.basket_action(quantity=1))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 1)
        self.interpreter.assert_not_called()

    def test_partial_removal_and_ambiguous_selection_followup(self):
        basket = self.store.get_basket()
        basket.items.append({**deepcopy(basket.items[0]), 'item_number': 9, 'size': 'Large'})
        self.store.set_basket(basket)
        action = self.basket_action('remove', {'by': 'name', 'value': 'Coffee'})
        reply, _ = self.run_actions('Remove the coffee', action)
        self.assertIn('#9', reply)
        self.assertEqual(len(self.store.get_basket().items), 2)
        pending = self.store.get_ongoing_queries()[0][-1]
        restored = pending.from_dict(pending.to_dict())
        self.assertIsNone(restored.resolved_action)
        self.assertIn('action_proposal', restored.basket_item)
        self.run_actions('Entry 9', self.basket_action('remove', {'by': 'id', 'value': '9'}),
                         reply_to=str(pending.query_id))
        self.assertEqual([row['item_number'] for row in self.store.get_basket().items], [1])
        self.run_actions('Remove it', self.basket_action('remove'))
        # Focus on the deleted entry must not jump to the remaining entry.
        self.assertEqual(len(self.store.get_basket().items), 1)

    def test_replacement_is_atomic_and_uses_current_catalog_prices(self):
        replacement = MenuItem.objects.create(tenant=self.tenant, name='Linen Notebook')
        variant = MenuItemVariant.objects.create(menu_item=replacement, size='A5', price='75')
        action = self.basket_action('replace', {'by': 'name', 'value': 'Coffee'},
                                    item_id=str(replacement.pk), variant_id=str(variant.pk))
        self.run_actions('Replace the coffee with a notebook', action)
        row = self.store.get_basket().items[0]
        self.assertEqual((row['item_number'], row['name'], row['quantity'], row['unit_price']),
                         (1, 'Linen Notebook', 1, '75.00'))
        invalid = self.basket_action('add', item_id='foreign', variant_id='missing', quantity=1)
        invalid.basket.lines[0].reference = None
        invalid.basket.lines.insert(0, self.basket_action('remove').basket.lines[0])
        before = deepcopy(self.store.get_basket().to_dict())
        basket = self.store.get_basket()
        resolved = resolve_action(invalid, basket=basket.items, focus=1)
        with self.assertRaises(ValueError):
            apply_proposal(resolved, basket, self.tenant.api_key)
        self.assertEqual(basket.to_dict(), before)

    def test_payment_continues_checkout_and_confirmation_is_bound_to_quote(self):
        self.run_actions('Yes, take the payment', ActionProposal(kind='CONTINUE_CHECKOUT'))
        self.assertEqual(self.store.get_checklist()['checkout']['awaiting'], 'mode')
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('I will collect it', ActionProposal(kind='SET_FULFILLMENT', value='pickup'),
                         reply_to=str(pending.query_id))
        self.assertFalse(Order.objects.exists())
        stale = resolve_action(ActionProposal(kind='CONFIRM_ORDER'), basket=self.store.get_basket().items,
                               checkout=self.store.get_checklist()['checkout'])
        basket = self.store.get_basket()
        basket.items[0]['quantity'] = 2
        _, order, _ = advance_checkout(tenant=self.tenant, customer=self.customer, chat_id='chat',
            platform='website', basket=basket, checklist={}, text='yes', configuration=self.config, action=stale)
        self.assertIsNone(order)
        self.assertFalse(Order.objects.exists())
        self.run_actions('Confirm my order', ActionProposal(kind='CONFIRM_ORDER'),
                         reply_to=str(pending.query_id))
        self.assertEqual(Order.objects.count(), 1)

    def test_saved_address_selection_ignores_negated_alternative(self):
        components = {'house_or_flat': '4', 'street_or_locality': 'Main Road', 'city': 'Delhi', 'state': 'Delhi', 'country': 'India', 'postal_code': '110001'}
        addresses = [CustomerAddress.objects.create(tenant=self.tenant, customer=self.customer,
            label=label, address_line='4 Main Road', components=components,
            location_coordinates=None) for label in ('Home', 'Office')]
        self.run_actions('Send it to Home, not Office', ActionProposal(kind='SELECT_ADDRESS',
                         reference={'by': 'name', 'value': 'Home'}))
        self.assertEqual(self.store.get_delivery_address()['address_id'], str(addresses[0].pk))
        self.assertFalse(self.store.get_checklist()['location'])
        self.assertFalse(Order.objects.exists())

    def test_typed_checkout_values_and_cancellation_survive_pending_round_trip(self):
        self.run_actions('Deliver it', ActionProposal(kind='SET_FULFILLMENT', value='delivery'))
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('42 Main Road', ActionProposal(kind='SET_CHECKOUT_FIELD', field='address', value='42 Main Road'),
                         reply_to=str(pending.query_id))
        self.assertEqual(self.store.get_checklist()['checkout']['fields']['address'], '42 Main Road')
        self.run_actions('No, leave it', ActionProposal(kind='CANCEL_PENDING_ACTION'),
                         reply_to=str(pending.query_id))
        self.session.refresh_from_db()
        self.assertFalse(self.session.state['checkout'])
        self.assertFalse(self.store.get_ongoing_queries()[0])
        self.assertEqual(len(self.store.get_basket().items), 1)
        self.assertFalse(Order.objects.exists())

    def test_add_more_of_focused_selection_preserves_variant(self):
        self.run_actions('Make it two', self.basket_action(quantity=2))
        self.run_actions('Add two more of those', self.basket_action('add', quantity=2))
        row = self.store.get_basket().items[0]
        self.assertEqual(row['quantity'], 4)
        self.assertEqual(row['item_variant_id'], str(self.variant.pk))

    def test_typed_payment_choice_still_obeys_store_policy(self):
        self.run_actions('Pickup', ActionProposal(kind='SET_FULFILLMENT', value='pickup'))
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        reply, _ = self.run_actions('Pay online', ActionProposal(kind='SET_PAYMENT_METHOD', value='online'),
                                    reply_to=str(pending.query_id))
        self.assertIn('Choose a payment method: cash', reply)
        self.assertEqual(self.store.get_checklist()['checkout']['payment_method'], 'cash')
        self.assertFalse(Order.objects.exists())

    def test_same_turn_confirmation_cannot_accept_a_new_quote(self):
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        reply, _ = self.run_actions('Pickup and confirm the order',
            ActionProposal(kind='SET_FULFILLMENT', value='pickup'), ActionProposal(kind='CONFIRM_ORDER'))
        self.assertIn('review the updated total', reply)
        self.assertFalse(Order.objects.exists())

    def test_basket_correction_answering_checkout_preserves_checkout_and_requotes(self):
        self.run_actions('Pickup', ActionProposal(kind='SET_FULFILLMENT', value='pickup'))
        self.run_actions('checkout', ActionProposal(kind='CONTINUE_CHECKOUT'))
        pending = self.store.get_ongoing_queries()[0][-1]
        self.run_actions('Actually make it three', self.basket_action(quantity=3),
                         reply_to=str(pending.query_id))
        self.assertEqual(self.store.get_basket().items[0]['quantity'], 3)
        self.assertIn(str(pending.query_id), [str(p.query_id) for p in self.store.get_ongoing_queries()[0]])
        self.session.refresh_from_db()
        self.assertNotIn('quote', self.session.state['checkout'])
        self.run_actions('Take payment', ActionProposal(kind='CONTINUE_CHECKOUT'),
                         reply_to=str(pending.query_id))
        self.assertEqual(self.store.get_checklist()['checkout']['quote']['total'], '305.00')
        self.assertFalse(Order.objects.exists())

    def test_mixed_basket_action_cannot_bypass_disabled_removal_capability(self):
        from chatbot_core.runtime_configuration import RuntimeConfiguration
        action = self.basket_action('remove')
        addition = self.basket_action('add', item_id=str(self.item.pk), variant_id=str(self.variant.pk), quantity=1)
        addition.basket.lines[0].reference = None
        action.basket.lines.extend(addition.basket.lines)
        before = deepcopy(self.store.get_basket().to_dict())
        allows = RuntimeConfiguration.allows
        with patch.object(RuntimeConfiguration, 'allows',
                          lambda config, intent, topic: topic != 'delete_entry' and allows(config, intent, topic)):
            self.run_actions('Remove the coffee and add one', action)
        self.assertEqual(self.store.get_basket().to_dict(), before)
