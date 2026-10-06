from copy import deepcopy
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from chatbot_core.models import TenantInfo
from chatbot_core.llm.schemas import ActionProposal
from chatbot_core.logic.action_resolver import resolve_action
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.checkout import service_time
from chatbot_core.logic.cafe.intent_handler.placing_order import PlacingOrderIntent
from orders.checkout_config import CheckoutPolicy, ModePolicy
from orders.models import CheckoutSettings, ChatSession, Customer, MenuItem, MenuItemVariant, Order
from users.checkout_forms import CheckoutSettingsForm
from users.models import TenantProfile


from tests.support.checkout import CheckoutFixture


class CheckoutTests(CheckoutFixture, TestCase):
    def partial_address_checkout(self):
        from chatbot_core.logic.cafe.workflow import graph, runner
        from tests.support.runtime import classification_result
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'delivery')
        pending = store.get_ongoing_queries()[0][-1]
        row = ('42 Main Street, Delhi', 'location_based', 'add_delivery_address',
               str(pending.query_id), 'Please share state, country and pincode.')
        with patch.object(graph, 'normalize_and_classify', return_value=classification_result([row])), \
                patch.object(graph, 'enqueue_string'), patch.object(runner, 'enqueue_string'), \
                patch('chatbot_core.logic.cafe.intent_handler.location_based.extract_address_with_gpt',
                      return_value={'street_address': '42 Main Street', 'city': 'Delhi'}):
            runner.run_conversation(self.tenant, store, row[0], self.customer)
        return store

    def test_partial_address_survives_postcode_payment_and_cache_recovery(self):
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore, _session_data
        store = self.partial_address_checkout()
        self.graph_turn(store, 'postal code: 110001')
        fields = store.get_checklist()['checkout']['fields']
        self.assertEqual(fields['address'], '42 Main Street, Delhi, 110001')
        self.assertEqual(fields['postal_code'], '110001')
        _session_data.clear()
        store = MemorySessionStore('chat', tenant_id=self.tenant.pk, platform='website')
        reply, _ = self.graph_turn(store, 'cash')
        self.assertIn('state, country', reply)
        self.assertNotIn('full address', reply)
        self.assertEqual(store.get_delivery_address(), {
            'street_address': '42 Main Street', 'city': 'Delhi', 'postal_code': '110001'})
        with patch('chatbot_core.logic.cafe.intent_handler.location_based.extract_address_with_gpt',
                   return_value={'state': 'Delhi', 'country': 'India'}):
            reply, _ = self.graph_turn(store, 'Delhi, India', ('location_based', 'add_delivery_address'))
        self.assertIn('Reply confirm', reply)
        self.assertFalse(Order.objects.exists())
        self.graph_turn(store, 'confirm')
        self.assertIn('42 Main Street', Order.objects.get().meta['checkout']['fields']['address'])

    def test_location_postcode_reply_merges_with_partial_checkout_address(self):
        store = self.partial_address_checkout()
        with patch('chatbot_core.logic.cafe.intent_handler.location_based.extract_address_with_gpt',
                   return_value={'postal_code': '110001'}) as extract:
            reply, _ = self.graph_turn(store, '110001', ('location_based', 'update_delivery_address'))
        self.assertIn('state, country', reply)
        self.assertIn('42 Main Street', store.get_checklist()['checkout']['fields']['address'])
        self.assertEqual(store.get_delivery_address()['city'], 'Delhi')
        self.assertEqual(extract.call_args.kwargs['original_text'], '110001')
        self.assertFalse(Order.objects.exists())

    def test_checkout_reuses_address_collected_before_fulfillment_selection(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        store.set_delivery_address({'street_address': '42 Main Street', 'city': 'Delhi',
                                    'state': 'Delhi', 'country': 'India'})
        self.graph_turn(store, 'checkout')
        reply, _ = self.graph_turn(store, 'delivery')
        self.assertIn('pincode', reply)
        self.assertEqual(store.get_checklist()['checkout']['awaiting'], 'postal_code')
        reply, _ = self.graph_turn(store, 'postal code: 110001')
        self.assertIn('Reply confirm', reply)
        self.assertIn('42 Main Street', reply)
        self.assertIn('Delhi', reply)

    def test_address_sync_invalidates_only_changed_details(self):
        from chatbot_core.logic.cafe.checkout import sync_delivery_address
        address = {'street_address': '42 Main Street', 'city': 'Delhi', 'state': 'Delhi',
                   'country': 'India', 'postal_code': '110001', 'address_id': 'selected-address'}
        self.turn('delivery')
        def sync(value):
            sync_delivery_address(tenant_id=self.tenant.pk, customer=self.customer, chat_id='chat',
                                  platform='website', checklist=self.checklist, address=value)
        sync(address)
        self.turn('checkout')
        original = deepcopy(self.checklist['checkout']['quote'])
        sync(address)
        self.assertEqual(self.checklist['checkout']['quote'], original)
        self.assertEqual(self.checklist['checkout']['address_components']['address_id'], 'selected-address')
        sync({**address, 'street_address': '99 New Street'})
        self.assertIsNone(self.checklist['checkout'].get('quote'))
        self.assertIsNone(self.turn('confirm')[1])
        self.assertEqual(self.turn('confirm')[1].meta['checkout']['fields']['address'],
                         '99 New Street, Delhi, 110001, India')

    def test_clearing_or_rejecting_postcode_retains_other_address_fields(self):
        store = self.partial_address_checkout()
        self.graph_turn(store, 'postal code: 110001')
        self.graph_turn(store, 'clear postcode', action=ActionProposal(kind='CLEAR_CHECKOUT_FIELD', field='postal_code'))
        self.assertNotIn('postal_code', store.get_delivery_address())
        self.assertEqual(store.get_delivery_address()['city'], 'Delhi')
        settings = CheckoutSettings.objects.get(tenant=self.tenant)
        settings.configuration['delivery_postal_codes'] = ['110001']
        settings.save()
        reply, _ = self.graph_turn(store, 'postal code: 999999')
        self.assertIn('outside', reply)
        self.assertNotIn('postal_code', store.get_delivery_address())
        self.assertEqual(store.get_delivery_address()['street_address'], '42 Main Street')
        self.assertIsNone(store.get_checklist()['checkout']['quote'])

    def test_rephrased_confirmation_cannot_replace_original_consent(self):
        self.turn('checkout')
        self.turn('pickup')
        reply, order, pending = self.turn('confirm', original_text='yes, maybe')
        self.assertIsNone(order)
        self.assertTrue(pending)
        self.assertFalse(Order.objects.exists())
        self.assertIn('Reply confirm', reply)

    def test_model_rejects_unsupported_combinations(self):
        bad_configs = []
        for field, value in [('modes', {}), ('timezone', 'Nowhere'), ('online_provider', 'imaginary'),
                             ('opening_hours', {'0': [['22:00', '09:00']]}),
                             ('opening_hours', {'7': [['09:00', '17:00']]})]:
            bad_configs.append({**deepcopy(self.config), field: value})
        for update in ({'required_fields': []}, {'required_fields': ['table_id']},
                       {'payment_methods': []}, {'payment_methods': ['online']},
                       {'fee': '-1'}, {'preparation_minutes': True}, {'minimum_order': 'NaN'}):
            config = deepcopy(self.config)
            config['modes']['delivery'].update(update)
            bad_configs.append(config)
        for config in bad_configs:
            with self.subTest(config=config), self.assertRaises(ValidationError):
                CheckoutSettings(tenant=self.tenant, configuration=config).save()
        self.assertFalse(CheckoutSettings.objects.exists())

    def test_unclassified_text_and_bare_payment_words_never_fill_fields_or_confirm(self):
        self.turn('delivery')
        for text in ('hmm', 'ok', '42 Main Street', 'pay', 'proceed'):
            self.assertIsNone(self.turn(text)[1])
            self.assertNotIn('address', self.checklist['checkout']['fields'])
        self.turn('address: 42 Main Street')
        for text in ('yes', 'pay', 'proceed', 'ok'):
            self.assertIsNone(self.turn(text)[1])
        self.assertFalse(Order.objects.exists())
        self.assertIsNotNone(self.turn('confirm')[1])

    def test_graph_unclear_reply_preserves_address_prompt(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'delivery')
        for text in ('hmm', 'ok'):
            self.graph_turn(store, text, ('insufficient_information', 'insufficient_information'))
            self.session.refresh_from_db()
            self.assertNotIn('address', self.session.state['checkout']['fields'])
            self.assertEqual(self.session.state['checkout']['awaiting'], 'address')
        self.assertIn('total: 130', self.graph_turn(store, '42 Main Street',
            ('location_based', 'confirm_delivery_address'), resolved='address: 42 Main Street')[0])

    def test_mode_and_scheduling_questions_do_not_start_checkout(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        with patch('chatbot_core.logic.cafe.intent_handler.placing_order.generate_response_from_knowledge', return_value='Ask the cafe.'):
            for text, topic in [('Do you offer delivery?', 'order_channels_and_modes'),
                                ('When is pickup?', 'order_scheduling')]:
                self.graph_turn(store, text, ('placing_order', topic))
                self.session.refresh_from_db()
                self.assertFalse(self.session.state.get('checkout'))
                self.assertFalse(store.get_checklist().get('checkout'))
        self.assertFalse(Order.objects.exists())

    def test_store_referrals_preserve_active_checkout_and_its_pending_question(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.session.refresh_from_db()
        before = deepcopy(self.session.state)
        pending = [p.to_dict() for p in store.get_ongoing_queries()[0]]
        basket = deepcopy(store.get_basket().to_dict())
        for topic, expected in PlacingOrderIntent.STORE_CONTACT_REPLIES.items():
            with self.subTest(topic=topic):
                action = ActionProposal(kind='SET_CHECKOUT_FIELD', field='scheduled_at',
                                        value='2026-09-30T19:00:00+05:30')
                reply, _ = self.graph_turn(store, 'Please arrange this',
                                          ('placing_order', topic), action=action)
                self.assertIn(expected, reply)
                self.session.refresh_from_db()
                self.assertEqual(self.session.state, before)
                self.assertEqual(store.get_basket().to_dict(), basket)
                self.assertEqual([p.to_dict() for p in store.get_ongoing_queries()[0]], pending)
        self.assertFalse(Order.objects.exists())

    def test_scheduling_action_cannot_bypass_store_referral_via_checkout_route(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        action = ActionProposal(kind='SET_CHECKOUT_FIELD', field='scheduled_at',
                                value='2026-09-30T19:00:00+05:30')
        reply, _ = self.graph_turn(store, 'Schedule for tomorrow', action=action)
        self.assertIn(PlacingOrderIntent.STORE_CONTACT_REPLIES['order_scheduling'], reply)
        self.assertFalse(store.get_checklist().get('checkout'))
        self.assertEqual(store.get_ongoing_queries(), ([], None))
        self.assertFalse(Order.objects.exists())

    def test_online_requires_explicit_provider_even_with_test_payments_enabled(self):
        self.config['modes']['pickup']['payment_methods'] = ['online']
        with self.assertRaises(ValidationError):
            CheckoutSettings(tenant=self.tenant, configuration=self.config).save()
        self.config['online_provider'] = 'adapter'
        with self.assertRaises(ValidationError):
            CheckoutSettings(tenant=self.tenant, configuration=self.config).save()

    def test_delivery_mode_switch_requotes_and_removes_address(self):
        self.assertIn('address', self.turn('delivery')[0])
        reply, order, _ = self.turn('address: 42 Main Street')
        self.assertIn('total: 130', reply)
        self.assertIsNone(order)
        reply, order, _ = self.turn('switch to pickup')
        self.assertIn('total: 105', reply)
        self.assertNotIn('address', self.checklist['checkout']['fields'])
        _, order, _ = self.turn('confirm')
        self.assertEqual(order.total_amount, Decimal('105'))
        self.assertEqual(order.order_type, 'P')
        self.assertFalse(order.enable_delivery)
        self.assertEqual(order.delivery_charges, 0)
        self.assertEqual(order.packing_charges, 5)
        self.assertEqual(order.payment_mode, 'cash')
        self.assertEqual(Order.objects.count(), 1)

    def test_dine_in_requires_table_but_no_delivery_details(self):
        self.assertIn('table', self.turn('dine-in')[0])
        self.assertIn('total: 100', self.turn('table: 12')[0])
        _, order, _ = self.turn('confirm')
        self.assertEqual(order.order_type, 'D')
        self.assertEqual(order.meta['checkout']['fields'], {'table_id': '12'})
        self.assertFalse(order.enable_delivery)

    def test_minimum_and_coverage_checked_before_order_creation(self):
        self.config['delivery_postal_codes'] = ['110001']
        self.config['modes']['delivery']['minimum_order'] = '200'
        self.turn('delivery')
        self.assertIn('postal', self.turn('address: 42 Main Street')[0])
        self.assertIn('outside', self.turn('postal code: 999999')[0])
        self.assertIn('minimum', self.turn('postal code: 110001')[0])
        self.assertFalse(Order.objects.exists())
        self.basket.items[0]['quantity'] = 2
        self.assertIn('total: 230', self.turn('checkout')[0])
        self.assertIsNotNone(self.turn('confirm')[1])

    def test_policy_and_cart_changes_need_fresh_confirmation(self):
        self.turn('pickup')
        self.config['modes']['pickup']['fee'] = '15'
        self.assertIn('total: 115', self.turn('confirm')[0])
        self.assertFalse(Order.objects.exists())
        self.basket.items[0]['quantity'] = 2
        self.assertIn('total: 215', self.turn('confirm')[0])
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.turn('confirm')[1].total_amount, Decimal('215'))

    def test_disabled_mode_on_resume_requires_selection(self):
        self.turn('pickup')
        del self.config['modes']['pickup']
        self.assertIn('Choose a fulfillment mode', self.turn('confirm')[0])
        self.assertFalse(Order.objects.exists())

    def test_lost_checklist_recovers_same_draft_and_order(self):
        self.turn('delivery')
        self.checklist = {}
        self.assertIn('total: 130', self.turn('address: 42 Main Street')[0])
        _, order, _ = self.turn('confirm')
        self.checklist = {}
        self.assertEqual(self.turn('confirm')[1].pk, order.pk)
        self.assertEqual(Order.objects.count(), 1)
        self.assertIn('call the store as soon as possible', self.turn('pickup')[0])
        order.refresh_from_db()
        self.assertEqual(order.order_type, 'H')

    def test_draft_is_scoped_to_customer_channel_and_tenant(self):
        other = Customer.objects.create(tenant=self.tenant, name='Other', phone='2222222222')
        self.customer = other
        with self.assertRaises(ValueError):
            self.turn('pickup')
        self.assertFalse(Order.objects.exists())

    def test_cancel_keeps_cart_and_clears_draft(self):
        self.turn('pickup')
        self.assertIn('stopped', self.turn('cancel checkout')[0])
        self.session.refresh_from_db()
        self.assertEqual(self.session.state['checkout'], {})
        self.assertFalse(self.basket.is_empty())
        self.assertFalse(Order.objects.exists())

    def test_live_catalog_price_change_blocks_confirmation(self):
        self.turn('pickup')
        self.variant.price = '110'
        self.variant.save()
        self.assertIn('price changed', self.turn('confirm')[0])
        self.assertFalse(Order.objects.exists())

    def test_scheduling_lead_time_horizon_hours_and_snapshot(self):
        now = datetime(2026, 9, 21, 4, 0, tzinfo=dt_timezone.utc)  # Monday 09:30 IST
        self.config['opening_hours'] = {'0': [['09:00', '22:00']]}
        self.config['modes']['pickup'].update(scheduling_enabled=True, required_fields=['scheduled_at'])
        with patch('chatbot_core.logic.cafe.checkout.timezone.now', return_value=now):
            self.assertIn('pickup time', self.turn('pickup')[0])
            self.assertIn('at least', self.turn('2026-09-21 09:40')[0])
            self.assertIn('closed', self.turn('2026-09-21 23:00')[0])
            self.assertIn('total: 105', self.turn('2026-09-21 12:00')[0])
            order = self.turn('confirm')[1]
        self.assertEqual(order.advanced_order, 'Y')
        self.assertEqual(str(order.preorder_time), '12:00:00')
        self.assertEqual(str(order.preorder_date), '2026-09-21')

    def test_optional_schedule_is_validated_before_payment_and_persistence(self):
        now = datetime(2026, 9, 29, 8, 30, tzinfo=dt_timezone.utc)  # Tuesday 14:00 IST
        self.config['opening_hours'] = {'2': [['09:00', '22:00']]}
        self.config['online_provider'] = 'adapter'
        self.config['modes']['pickup'].update(
            scheduling_enabled=True, preparation_minutes=30, max_advance_days=7,
            payment_methods=['cash', 'online'])

        def schedule(value):
            action = resolve_action(ActionProposal(
                kind='SET_CHECKOUT_FIELD', field='scheduled_at', value=value),
                basket=self.basket.items, checkout=self.checklist.get('checkout'))
            return self.turn(value, action=action)

        with patch('chatbot_core.logic.cafe.checkout.timezone.now', return_value=now):
            self.assertIn('payment method', self.turn('pickup')[0])
            for value, error in (
                    ('2026-09-28T19:00:00', 'at least'),
                    ('2026-09-29T14:10:00', 'at least'),
                    ('2026-10-01T19:00:00', 'closed'),
                    ('2026-10-07T19:00:00', 'within 7 days')):
                with self.subTest(value=value):
                    self.assertIn(error, schedule(value)[0])
                    self.session.refresh_from_db()
                    self.assertNotIn('scheduled_at', self.checklist['checkout']['fields'])
                    self.assertNotIn('scheduled_at', self.session.state['checkout']['fields'])

            self.assertIn('payment method', schedule('2026-09-30T19:00:00')[0])
            self.assertEqual(self.checklist['checkout']['fields']['scheduled_at'], '2026-09-30T19:00:00')
            self.assertIn('Reply confirm', self.turn('cash')[0])
            self.assertIsNotNone(self.checklist['checkout']['quote'])

            self.assertIn('at least', schedule('2026-09-28T19:00:00')[0])
            self.session.refresh_from_db()
            self.assertNotIn('scheduled_at', self.session.state['checkout']['fields'])
            self.assertIsNone(self.session.state['checkout']['quote'])

    def test_postal_coverage_is_validated_before_payment_and_persistence(self):
        self.config['delivery_postal_codes'] = ['110001']
        self.config['online_provider'] = 'adapter'
        self.config['modes']['delivery']['payment_methods'] = ['cash', 'online']
        self.turn('delivery')
        self.turn('address: 42 Main Street')

        self.assertIn('outside', self.turn('postal code: 999999')[0])
        self.session.refresh_from_db()
        self.assertNotIn('postal_code', self.checklist['checkout']['fields'])
        self.assertNotIn('postal_code', self.session.state['checkout']['fields'])

        self.assertIn('payment method', self.turn('postal code: 110001')[0])
        self.assertEqual(self.checklist['checkout']['fields']['postal_code'], '110001')

    def test_closed_immediate_checkout_and_dst_validation(self):
        policy = CheckoutPolicy.model_validate({**self.config, 'opening_hours': {'0': [['09:00', '10:00']]}})
        with self.assertRaisesMessage(ValueError, 'closed'):
            service_time(policy, policy.modes['pickup'], {}, datetime(2026, 9, 21, 4, 25, tzinfo=dt_timezone.utc))
        policy.timezone = 'America/New_York'
        policy.modes['pickup'].scheduling_enabled = True
        for date in ['2026-03-08 02:30', '2026-11-01 01:30']:
            with self.assertRaisesMessage(ValueError, 'unambiguous'):
                service_time(policy, policy.modes['pickup'], {'scheduled_at': date}, datetime(2026, 3, 7, tzinfo=dt_timezone.utc))

    def test_handler_cash_never_initiates_online_payment(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        intent = PlacingOrderIntent(main_query='pickup', sub_intent='order_channels_and_modes', tenant=self.tenant.pk, chat_id='chat')
        intent.platform = 'website'
        args = self.basket, {}, self.checklist, [], self.tenant.api_key, self.customer
        with patch('chatbot_core.logic.cafe.intent_handler.placing_order.initiate_payment') as pay:
            intent.sub_intent = 'order_confirmation'
            intent.main_query = intent.original_query = 'checkout'
            intent.process_query(*args)
            intent.main_query = intent.original_query = 'pickup'
            intent.process_query(*args)
            intent.main_query = intent.original_query = 'confirm'
            self.assertIn('Pay cash', intent.process_query(*args)[0])
            pay.assert_not_called()

    def test_dashboard_is_tenant_scoped_and_errors_do_not_save(self):
        user = User.objects.create_user(username='checkout-owner')
        TenantProfile.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)
        url = reverse('tenant:tenant_settings')
        self.assertContains(self.client.get(url), 'Save checkout and hours')
        data = {'section': 'checkout', 'modes': ['pickup'], 'timezone': 'Asia/Kolkata',
                'always_open': 'on', 'pickup_payment_methods': ['cash'],
                'pickup_preparation_minutes': 15, 'pickup_fee': '10', 'pickup_max_advance_days': 5}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        saved = CheckoutSettings.objects.get(tenant=self.tenant)
        self.assertEqual(saved.configuration['modes']['pickup']['fee'], '10')
        data['pickup_payment_methods'] = ['online']
        self.assertEqual(self.client.post(url, data).status_code, 400)
        saved.refresh_from_db()
        self.assertEqual(saved.configuration['modes']['pickup']['payment_methods'], ['cash'])
        self.assertEqual(CheckoutSettings.objects.count(), 1)


    def test_contextual_checkout_values_reach_fields_without_explanatory_prose(self):
        self.config['modes']['pickup']['required_fields'] = ['name', 'phone']
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, "I'll collect it myself", resolved='pickup')
        self.assertEqual(store.get_checklist()['checkout']['mode'], 'pickup')
        self.graph_turn(store, 'मेरा नाम अनाया है', resolved='name: अनाया')
        self.graph_turn(store, 'Use +44 7700 900123', resolved='phone: +44 7700 900123')
        self.assertEqual(store.get_checklist()['checkout']['fields'],
                         {'name': 'अनाया', 'phone': '+44 7700 900123'})
        self.graph_turn(store, 'confirm')
        self.assertEqual(Order.objects.get().meta['checkout']['fields']['name'], 'अनाया')

    def test_unlabelled_contextual_prose_cannot_be_saved_as_a_checkout_field(self):
        self.config['modes']['pickup']['required_fields'] = ['name']
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.graph_turn(store, 'Alice', resolved='Use Alice as the name for this order.')
        self.assertNotIn('name', store.get_checklist()['checkout']['fields'])
        self.assertFalse(Order.objects.exists())
        self.graph_turn(store, 'Alice', resolved='name: Alice')
        self.assertEqual(store.get_checklist()['checkout']['fields']['name'], 'Alice')

    def test_rejected_address_cannot_be_confirmed_or_recovered(self):
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore, _session_data
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'delivery')
        self.graph_turn(store, 'address: 42 Old Street')
        self.graph_turn(store, 'Do not use that address', ('location_based', 'deny_delivery_address'))
        self.session.refresh_from_db()
        self.assertNotIn('address', self.session.state['checkout']['fields'])
        self.assertIsNone(self.session.state['checkout']['quote'])
        self.assertIn('review the total', self.graph_turn(store, 'confirm')[0])
        self.assertFalse(Order.objects.exists())
        _session_data.clear()
        store = MemorySessionStore('chat', tenant_id=self.tenant.pk, platform='website')
        self.assertIn('review the total', self.graph_turn(store, 'confirm')[0])
        self.graph_turn(store, 'address: 99 New Street')
        self.graph_turn(store, 'confirm')
        self.assertEqual(Order.objects.get().meta['checkout']['fields']['address'], '99 New Street')

    def test_address_management_commands_never_become_checkout_fields(self):
        from chatbot_core.logic.cafe.intent_handler.location_based import LocationBasedIntent
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        for sub_intent in ('deny_delivery_address', 'change_delivery_address',
                           'update_delivery_address', 'add_delivery_address'):
            for mode, awaiting in (('delivery', 'address'), ('delivery', 'name'),
                                   ('delivery', 'phone'), ('dine_in', 'table_id')):
                with self.subTest(sub_intent=sub_intent, mode=mode, awaiting=awaiting):
                    self.session.state = {'checkout': {'mode': mode, 'awaiting': awaiting,
                        'fields': {'address': 'Old Street'} if mode == 'delivery' else {},
                        'basket': self.basket.to_dict(), 'quote': {'fingerprint': 'old'}}}
                    self.session.save()
                    checklist = {'checkout': deepcopy(self.session.state['checkout'])}
                    intent = PlacingOrderIntent(main_query='checkout', sub_intent='order_confirmation',
                        tenant=self.tenant.pk, chat_id='chat', basket_item={'checkout': True})
                    intent.platform = 'website'
                    incoming = LocationBasedIntent(main_query='Please change my address',
                        sub_intent=sub_intent, tenant=self.tenant.pk, chat_id='chat')
                    incoming.platform = 'website'
                    intent.process_followup(incoming, self.basket, {}, checklist, [], self.tenant.api_key, self.customer)
                    self.session.refresh_from_db()
                    draft = self.session.state['checkout']
                    self.assertEqual(draft['fields'], {})
                    self.assertIsNone(draft['quote'])
                    self.assertIsNone(self.turn('confirm')[1])
        self.assertFalse(Order.objects.exists())

    def test_address_change_requests_require_replacement_before_confirmation(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'delivery')
        for sub_intent in ('update_delivery_address', 'add_delivery_address'):
            with self.subTest(sub_intent=sub_intent):
                self.graph_turn(store, 'address: Old Street')
                self.graph_turn(store, 'Change my address', ('location_based', sub_intent))
                self.assertIn('review the total', self.graph_turn(store, 'confirm')[0])
                self.session.refresh_from_db()
                self.assertEqual(self.session.state['checkout']['fields'], {})
                self.assertFalse(Order.objects.exists())
        self.graph_turn(store, 'address: New Street')
        self.graph_turn(store, 'confirm')
        self.assertEqual(Order.objects.get().meta['checkout']['fields']['address'], 'New Street')

    def test_explicit_replacement_address_is_accepted_but_not_confirmed(self):
        from chatbot_core.logic.cafe.intent_handler.location_based import LocationBasedIntent
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.turn('delivery')
        self.turn('address: Old Street')
        intent = PlacingOrderIntent(main_query='checkout', sub_intent='order_confirmation',
            tenant=self.tenant.pk, chat_id='chat', basket_item={'checkout': True})
        intent.platform = 'website'
        incoming = LocationBasedIntent(main_query='address: 99 New Street',
            sub_intent='update_delivery_address', tenant=self.tenant.pk, chat_id='chat')
        incoming.platform = 'website'
        reply, _ = intent.process_followup(incoming, self.basket, {}, self.checklist, [],
                                           self.tenant.api_key, self.customer)
        self.assertIn('Reply confirm', reply)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.checklist['checkout']['fields']['address'], '99 New Street')
        self.assertEqual(self.turn('confirm')[1].meta['checkout']['fields']['address'], '99 New Street')

    def test_information_preserves_item_and_address_tasks(self):
        from chatbot_core.logic.cafe.intent_handler.location_based import LocationBasedIntent
        store = self.graph_store()
        for cls, sub_intent, fields in (
                (PlacingOrderIntent, 'add_to_basket', {'name': 'Coffee', 'quantity': 2}),
                (LocationBasedIntent, 'add_delivery_address', {})):
            with self.subTest(sub_intent=sub_intent):
                pending = cls(main_query='unfinished request', sub_intent=sub_intent,
                    tenant=self.tenant.pk, chat_id='chat', basket_item=fields,
                    follow_up_question=['Please finish this request.'])
                pending.platform = 'website'
                store.set_ongoing_queries([pending], 0)
                with patch('chatbot_core.logic.cafe.workflow.graph.enqueue_string'), \
                        patch('chatbot_core.logic.cafe.intent_handler.information_about_the_cafe.generate_response_from_knowledge', return_value='Open until 8pm.'):
                    for _ in range(2):
                        reply, _ = self.graph_turn(store, 'When do you close?',
                                                  ('information_about_the_cafe', 'location_and_hours'))
                        self.assertIn('8pm', reply)
                        saved, index = store.get_ongoing_queries()
                        self.assertEqual((len(saved), index), (1, 0))
                        saved_fields = deepcopy(saved[0].basket_item)
                        if sub_intent == 'add_to_basket':
                            self.assertEqual(saved_fields.pop('clarification_budget'), {
                                'delivered': 0,
                                'progress': {'lines': [], 'unresolved': 0, 'catalog_miss': False},
                            })
                            self.assertEqual(saved[0].ignored_count, 0)
                        self.assertEqual(saved_fields, fields)
                        self.assertEqual(saved[0].main_query, pending.main_query)

    def test_completed_order_can_start_fresh_order_in_same_chat(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.graph_turn(store, 'confirm')
        old_order = Order.objects.get()
        Order.objects.update(payment_status=Order.PaymentStatus.PAID, order_status=Order.Status.DELIVERED)
        self.assertIn('new order', self.graph_turn(store, 'start a new order', ('placing_order', 'initiate_order'))[0])
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_completed)
        self.assertTrue(store.get_basket().is_empty())
        self.assertFalse(store.get_checklist().get('order'))
        self.assertFalse(store.get_ongoing_queries()[0])
        store.set_basket(deepcopy(self.basket))
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.graph_turn(store, 'confirm')
        self.assertEqual(Order.objects.count(), 2)
        self.assertEqual(self.session.order_id, old_order.pk)
        self.assertEqual(ChatSession.objects.filter(is_completed=False).count(), 1)

    def test_new_order_with_items_applies_to_fresh_basket(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.graph_turn(store, 'confirm')
        Order.objects.update(payment_status=Order.PaymentStatus.PAID, order_status=Order.Status.DELIVERED)
        menu = {self.tenant.api_key: {'Coffee': {
            'item_id': str(self.item.pk), 'item_variant_map': {'Regular': str(self.variant.pk)},
            'pricing': {str(self.variant.pk): '100'}}}}
        from tests.support.actions import change_action
        action = change_action({'lines': [{'action': 'add', 'item_id': str(self.item.pk),
            'variant_id': str(self.variant.pk), 'quantity': 1, 'modifiers': [],
            'target_number': None, 'unresolved': []}], 'unresolved': [], 'catalog_miss': False})
        with patch('chatbot_core.logic.cafe.basket.get_item_pricing_cache', return_value=menu):
            reply, _ = self.graph_turn(store, 'start a new order with one coffee',
                                       ('placing_order', 'initiate_order'), action=action)
        self.assertIn('Added 1', reply)
        self.assertEqual(store.get_basket().items[0]['quantity'], 1)
        self.assertFalse(store.get_checklist().get('order'))
        self.session.refresh_from_db()
        self.assertTrue(self.session.is_completed)

    def test_new_order_does_not_discard_active_order(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.graph_turn(store, 'confirm')
        self.assertIn('still active', self.graph_turn(store, 'start a new order', ('placing_order', 'initiate_order'))[0])
        self.assertEqual(ChatSession.objects.count(), 1)
        self.assertTrue(store.get_checklist()['order'])

    def test_new_order_transition_recovers_after_cache_save_failure(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.graph_turn(store, 'confirm')
        Order.objects.update(payment_status=Order.PaymentStatus.PAID, order_status=Order.Status.DELIVERED)
        with patch.object(store, 'publish_snapshot', side_effect=ConnectionError('cache unavailable')):
            with self.assertRaises(ConnectionError):
                self.graph_turn(store, 'start a new order', ('placing_order', 'initiate_order'))
        self.graph_turn(store, 'show basket', ('placing_order', 'check_order_cart'))
        self.assertTrue(store.get_basket().is_empty())
        self.assertFalse(store.get_checklist().get('order'))
        self.assertFalse(store.get_checklist().get('checkout'))
        self.assertEqual(ChatSession.objects.filter(is_completed=False).count(), 1)
        self.assertEqual(Order.objects.count(), 1)

    def test_confirmation_cannot_bypass_separate_address_task(self):
        from chatbot_core.logic.cafe.intent_handler.location_based import LocationBasedIntent
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'delivery')
        self.graph_turn(store, 'address: Old Street')
        pending, _ = store.get_ongoing_queries()
        address = LocationBasedIntent(main_query='replace address', sub_intent='add_delivery_address',
            tenant=self.tenant.pk, chat_id='chat', follow_up_question=['What is the new address?'])
        address.platform = 'website'
        pending.append(address)
        store.set_ongoing_queries(pending, len(pending) - 1)
        self.assertIn('finish or cancel', self.graph_turn(store, 'confirm')[0])
        self.assertFalse(Order.objects.exists())
        self.assertEqual(len(store.get_ongoing_queries()[0]), 2)

    def test_partial_save_failure_leaves_entire_session_unchanged(self):
        from chatbot_core.logic.cafe.workflow.runner import load_state, save_state
        store = self.graph_store()
        before = deepcopy(store.read_snapshot())
        state = load_state(store, self.tenant, 'change order and address')
        state['basket'].items[0]['quantity'] = 7
        state['delivery_address'] = {'city': 'New city'}
        with patch.object(store, 'set_delivery_address', side_effect=RuntimeError('store unavailable')):
            with self.assertRaisesMessage(RuntimeError, 'store unavailable'):
                save_state(store, state)
        self.assertEqual(store.read_snapshot(), before)

    def test_graph_recovers_after_cache_loss_and_reuses_confirmed_order(self):
        from chatbot_core.logic.cafe.session.memory import MemorySessionStore, _session_data
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'checkout')
        self.assertIn('total: 105', self.graph_turn(store, 'pickup')[0])
        _session_data.clear()
        store = MemorySessionStore('chat', tenant_id=self.tenant.pk, platform='website')
        self.assertIn('Pay cash', self.graph_turn(store, 'confirm')[0])
        self.assertEqual(Order.objects.count(), 1)
        _session_data.clear()
        store = MemorySessionStore('chat', tenant_id=self.tenant.pk, platform='website')
        self.assertIn('Pay cash', self.graph_turn(store, 'confirm')[0])
        self.assertEqual(Order.objects.count(), 1)

    def test_graph_replies_resume_fields_and_switch_modes(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.assertIn('address', self.graph_turn(store, 'delivery')[0])
        self.assertIn('total: 130', self.graph_turn(store, 'address: 42 Main Street')[0])
        self.assertIn('table', self.graph_turn(store, 'dine-in')[0])
        self.assertIn('total: 100', self.graph_turn(store, 'table: 12')[0])
        self.assertIn('Pay cash', self.graph_turn(store, 'confirm')[0])
        self.assertEqual(Order.objects.get().meta['checkout']['fields'], {'table_id': '12'})

    def test_graph_cancel_does_not_resurrect_draft(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.assertIn('stopped', self.graph_turn(store, 'cancel', ('general', 'cancel_and_abort'))[0])
        self.session.refresh_from_db()
        self.assertFalse(self.session.state['checkout'])
        self.assertFalse(store.get_ongoing_queries()[0])
        self.assertFalse(Order.objects.exists())
        self.graph_turn(store, 'confirm')
        self.assertFalse(Order.objects.exists())

    def test_checkout_reply_to_cancel_clarification_resumes_saved_checkout(self):
        from chatbot_core.logic.cafe.intent_handler.order_enquiry import OrderEnquiryIntent
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        pending, _ = store.get_ongoing_queries()
        original = pending[-1]
        clarification = OrderEnquiryIntent(main_query='cancel', sub_intent='refund_and_cancellation',
            tenant=self.tenant.pk, chat_id='chat', query_id=999,
            basket_item={'clarify_cancel_target': True, 'cancel_task_id': original.query_id},
            follow_up_question=[OrderEnquiryIntent.CANCEL_TARGET_RESPONSE])
        clarification.platform = 'website'
        store.set_ongoing_queries([*pending, clarification], len(pending))
        before = deepcopy(store.get_basket().to_dict())
        response, _ = self.graph_turn(store, 'checkout', ('placing_order', 'order_confirmation'))
        self.assertNotIn('stopped', response.lower())
        self.assertIn('total:', response)
        self.assertEqual(store.get_basket().to_dict(), before)
        pending, _ = store.get_ongoing_queries()
        self.assertTrue(any(p.basket_item.get('checkout') for p in pending))
        self.session.refresh_from_db()
        self.assertTrue(self.session.state['checkout'])

    def test_polite_cancel_stops_checkout_and_preserves_basket(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        before = deepcopy(store.get_basket().to_dict())
        response, _ = self.graph_turn(store, 'please cancel', ('general', 'cancel_and_abort'))
        self.assertIn('stopped', response)
        self.assertEqual(store.get_basket().to_dict(), before)
        self.assertEqual(store.get_ongoing_queries(), ([], None))
        self.session.refresh_from_db()
        self.assertFalse(self.session.state['checkout'])

    def test_graph_preserves_intentionally_emptied_cart(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        store.clear_basket()
        self.assertIn('empty', self.graph_turn(store, 'confirm')[0])
        self.assertFalse(Order.objects.exists())
        self.session.refresh_from_db()
        self.assertEqual(self.session.state['checkout']['basket']['items'], [])


    def test_committed_cancellation_clears_stale_chat_cache(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        self.graph_turn(store, 'name: Old checkout contact')
        self.assertEqual(store.get_checklist()['checkout']['fields']['name'], 'Old checkout contact')
        self.turn('cancel checkout')  # Simulate a lost cache save after DB commit.
        self.assertIn('Pickup', self.graph_turn(store, 'checkout')[0])
        self.assertFalse(Order.objects.exists())
        draft = store.get_checklist()['checkout']
        self.assertEqual(draft['mode'], 'pickup')  # The preference survives; the old draft does not.
        self.assertNotEqual(draft['fields'].get('name'), 'Old checkout contact')
        self.session.refresh_from_db()
        self.assertEqual(self.session.state['checkout'], draft)

    def test_newer_session_for_other_customer_cannot_reuse_old_checkout(self):
        other = Customer.objects.create(tenant=self.tenant, name='Other', phone='2222222222')
        ChatSession.objects.create(tenant=self.tenant, customer=other, session_id='chat', platform='website')
        with self.assertRaises(ValueError):
            self.turn('pickup')
        self.assertFalse(Order.objects.exists())

    def test_daily_hours_form_and_model_validate_overnight_split(self):
        data = {'modes': ['pickup'], 'timezone': 'Asia/Kolkata', 'hours_0': '18:00-24:00',
                'hours_1': '00:00-02:00', 'pickup_payment_methods': ['cash']}
        form = CheckoutSettingsForm(data)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.configuration['opening_hours']['0'], [['18:00', '24:00']])
        self.assertEqual(form.configuration['opening_hours']['2'], [])
        data['hours_0'] = '22:00-02:00'
        self.assertFalse(CheckoutSettingsForm(data).is_valid())

    def test_switch_mode_resets_online_selection_and_recovery_checks_scheduling(self):
        self.config['online_provider'] = 'adapter'
        self.config['modes']['delivery']['payment_methods'] = ['online']
        self.turn('delivery')
        self.turn('address: 42 Main Street')
        self.assertEqual(self.checklist['checkout']['payment_method'], 'online')
        self.turn('pickup')
        self.assertEqual(self.checklist['checkout']['payment_method'], 'cash')
        self.assertEqual(self.turn('confirm')[1].payment_mode, 'cash')

    def test_scheduled_time_is_rechecked_after_draft_recovery(self):
        self.config['modes']['pickup'].update(scheduling_enabled=True)
        now = datetime(2026, 9, 21, 4, 0, tzinfo=dt_timezone.utc)
        with patch('chatbot_core.logic.cafe.checkout.timezone.now', return_value=now):
            self.turn('pickup')
            self.turn('2026-09-21 10:00')
        self.checklist = {}
        with patch('chatbot_core.logic.cafe.checkout.timezone.now', return_value=now + timedelta(hours=1)):
            self.assertIn('at least', self.turn('confirm')[0])
        self.assertFalse(Order.objects.exists())

    def test_confirmation_cannot_skip_pending_basket_change(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'pickup')
        pending, _ = store.get_ongoing_queries()
        item_change = PlacingOrderIntent(main_query='add coffee', sub_intent='add_to_basket',
            tenant=self.tenant.pk, chat_id='chat', basket_item={'name': 'Coffee'},
            follow_up_question=['Which size?'])
        item_change.platform = 'website'
        pending.append(item_change)
        store.set_ongoing_queries(pending, len(pending) - 1)
        self.assertIn('finish or cancel', self.graph_turn(store, 'confirm')[0])
        self.assertFalse(Order.objects.exists())

    def test_cart_question_keeps_unfinished_checkout(self):
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        store = self.graph_store()
        self.graph_turn(store, 'checkout')
        self.graph_turn(store, 'dine-in')
        self.graph_turn(store, 'show my basket', ('placing_order', 'check_order_cart'))
        self.session.refresh_from_db()
        self.assertEqual(self.session.state['checkout']['awaiting'], 'table_id')
        self.assertTrue(store.get_ongoing_queries()[0][-1].basket_item['checkout'])
        self.assertIn('review the total', self.graph_turn(store, 'confirm')[0])
        self.assertFalse(Order.objects.exists())


class PayableLimitTests(TestCase):
    """The payable cap is checked after fees, on both pricing paths."""

    def setUp(self):
        from tests.support.ordering import seed_evaluation_policy
        self.tenant = TenantInfo.objects.create(display_name='Payable Cafe', approval_status='APPROVED')
        self.config = seed_evaluation_policy(self.tenant)
        self.item = MenuItem.objects.create(tenant=self.tenant, name='Payable')
        self.variant = MenuItemVariant.objects.create(menu_item=self.item, size='Cup', price='4000.00')

    def _quote(self, price, fee, *, commerce):
        from chatbot_core.logic.cafe.checkout import quote
        self.variant.price = price
        self.variant.save()
        self.config.enabled = commerce
        self.config.save()
        basket = Basket(items=[{
            'item_id': str(self.item.pk), 'item_variant_id': str(self.variant.pk),
            'name': 'Payable', 'size': 'Cup', 'quantity': 1, 'unit_price': str(price),
            'item_number': 1, 'modifiers': [],
        }])
        policy = CheckoutPolicy(modes={'pickup': ModePolicy(
            required_fields=[], payment_methods=['cash'], fee=Decimal(fee))})
        return quote(policy, {'mode': 'pickup', 'payment_method': 'cash', 'fields': {}}, basket, self.tenant)

    def test_exact_payable_cap_succeeds_and_one_minor_unit_above_fails(self):
        for commerce in (False, True):
            with self.subTest(commerce=commerce):
                details, field, question = self._quote('4000.00', '2000.00', commerce=commerce)
                self.assertIsNone(field)
                self.assertEqual(Decimal(details['total']), Decimal('6000.00'))
                if commerce:
                    self.assertEqual(details['commerce']['total_minor'], 600000)
                    self.assertEqual(details['commerce']['subtotal_minor'], 400000)
                with self.assertRaisesRegex(ValueError, 'payable limit'):
                    self._quote('4000.00', '2000.01', commerce=commerce)

    def test_commerce_and_catalog_totals_match_without_tax(self):
        from commerce.pricing import calculate
        from chatbot_core.logic.cafe.ordering_limits import basket_subtotal_minor
        disabled, _, _ = self._quote('10.05', '0', commerce=False)
        enabled, _, _ = self._quote('10.05', '0', commerce=True)
        self.assertEqual(Decimal(disabled['total']), Decimal(enabled['total']))
        self.assertEqual(enabled['commerce']['subtotal_minor'], basket_subtotal_minor(
            [{'quantity': 1, 'unit_price': '10.05', 'modifiers': []}], 2))
        priced = calculate([{'item_id': 'a', 'quantity': 1, 'unit_price': '10.05', 'modifiers': []}],
                           self.config.policy, mode='pickup', fee='0')
        self.assertEqual(priced['subtotal_minor'], enabled['commerce']['subtotal_minor'])
        self.assertEqual(priced['schema_version'], 1)
