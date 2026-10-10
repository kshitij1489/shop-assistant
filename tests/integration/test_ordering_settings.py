"""Form-driven setup, currency conversion and provider-free order acceptance."""
import io
import os
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from chatbot_core.models import TenantInfo, TenantJSONDoc, TenantRuntimeConfiguration
from chatbot_core.runtime_configuration import get_configuration, publish_default_configuration
from commerce.forms import CommerceSettingsForm
from commerce.models import Configuration, Connection, StockItem, Command, ReconciliationIssue
from commerce.policy import Policy
from orders.checkout_config import default_checkout_config
from orders.models import CheckoutSettings, MenuItem, MenuItemVariant, Customer, ChatSession
from orders.onboarding import initialize_ordering_settings, complete_ordering_setup
from orders.settings_defaults import ordering_defaults
from tests.support.ordering import ordering_form_data
from users.models import TenantProfile


@override_settings(SIGNUP_ALERT_EMAIL='', LEGACY_TENANT_SYNC_ENABLED=False)
class OrderingSettingsTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Form café', approval_status='APPROVED')
        self.user = get_user_model().objects.create_user('form-owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.url = reverse('tenant:tenant_settings')

    def menu(self):
        item = MenuItem.objects.create(tenant=self.tenant, name='Coffee')
        return MenuItemVariant.objects.create(menu_item=item, size='Regular', price='100')

    def setup_data(self):
        return {'section': 'ordering_setup', 'setup-modes': ['pickup'], 'setup-timezone': 'Asia/Kolkata',
                'setup-days': ['0', '1', '2', '3', '4', '5', '6'], 'setup-opens': '09:00',
                'setup-closes': '18:00', 'setup-confirmed': 'on'}

    def test_signup_saves_complete_defaults_before_publication(self):
        response = self.client.post(reverse('signup'), dict(username='new-form-owner', email='a@example.org',
            password='test-owner-password', password2='test-owner-password', business_name='New form cafe', business_type='cafe'))
        self.assertEqual(response.status_code, 302)
        tenant = TenantInfo.objects.get(display_name='New form cafe')
        defaults = ordering_defaults()
        self.assertEqual(CheckoutSettings.objects.get(tenant=tenant).configuration, defaults['checkout'])
        config = Configuration.objects.get(tenant=tenant)
        self.assertEqual(config.policy, defaults['policy'])
        self.assertFalse(config.local_checkout)
        self.assertFalse(config.enabled)
        self.assertTrue(tenant.meta['ordering_setup_required'])
        self.assertTrue(get_configuration(tenant_id=tenant.pk).allows('general', 'greeting'))
        self.assertFalse(get_configuration(tenant_id=tenant.pk).allows('placing_order', 'order_confirmation'))
        self.assertFalse(Connection.objects.filter(location__tenant=tenant).exists())

    def test_open_settings_persists_missing_records_and_retains_existing_values(self):
        response = self.client.get(self.url + '?tab=ordering')
        self.assertContains(response, 'Ordering rules')
        self.assertContains(response, 'Add rule', count=2)
        self.assertNotContains(response, 'name="taxes"')
        checkout = CheckoutSettings.objects.get(tenant=self.tenant)
        config = Configuration.objects.get(tenant=self.tenant)
        checkout.configuration['modes']['pickup']['fee'] = '27.50'
        checkout.save()
        config.policy = Policy(currency='JPY', exponent=0, ordering_limits=None).model_dump(mode='json')
        config.local_checkout = False
        config.save()
        self.client.get(self.url)
        checkout.refresh_from_db(); config.refresh_from_db()
        self.assertEqual(checkout.configuration['modes']['pickup']['fee'], '27.50')
        self.assertEqual(config.policy['currency'], 'JPY')
        self.assertIsNone(config.policy['ordering_limits'])
        self.assertFalse(config.local_checkout)

    def test_older_sparse_records_display_without_replacing_saved_values(self):
        checkout, config = initialize_ordering_settings(self.tenant)
        checkout.configuration = {'modes': {'pickup': {'fee': '8.50'}}}
        checkout.save()
        config.policy = {'currency': 'INR', 'packaging_minor': 125}
        config.save()
        self.assertContains(self.client.get(self.url), 'value="8.50"')
        self.assertContains(self.client.get(self.url + '?tab=ordering'), 'value="1.25"')
        checkout.refresh_from_db(); config.refresh_from_db()
        self.assertEqual(checkout.configuration, {'modes': {'pickup': {'fee': '8.50'}}})
        self.assertEqual(config.policy, {'currency': 'INR', 'packaging_minor': 125})

    def test_settings_and_exports_preserve_legacy_coverage_before_setup(self):
        from chatbot_core.logic.cafe.db_utils import verify_delivery_pincode
        self.tenant.meta = {'serviceable_pincodes': ['560001']}
        self.tenant.save()
        for query in ('?download=checkout', '?download=ordering', '?tab=ordering'):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.url + query).status_code, 200)
                self.tenant.refresh_from_db()
                self.assertFalse(Configuration.objects.get(tenant=self.tenant).local_checkout)
                self.assertTrue(verify_delivery_pincode(self.tenant, '560001'))
                self.assertFalse(verify_delivery_pincode(self.tenant, '110001'))
        # Saving rules can adopt pricing before setup, but must not switch coverage.
        self.assertEqual(self.client.post(self.url, {**ordering_form_data(), 'section': 'ordering'}).status_code, 302)
        self.assertTrue(Configuration.objects.get(tenant=self.tenant).local_checkout)
        self.assertTrue(verify_delivery_pincode(self.tenant, '560001'))
        self.assertFalse(verify_delivery_pincode(self.tenant, '110001'))

    def test_existing_unrestricted_delivery_keeps_legacy_coverage_until_setup(self):
        from chatbot_core.logic.cafe.db_utils import verify_delivery_pincode
        self.tenant.meta = {'serviceable_pincodes': ['560001']}
        self.tenant.save()
        checkout = ordering_defaults(demo=True)['checkout']
        checkout['delivery_postal_codes'] = []
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=checkout)
        self.client.get(self.url)
        self.tenant.refresh_from_db()
        self.assertFalse(verify_delivery_pincode(self.tenant, '110001'))
        self.assertTrue(verify_delivery_pincode(self.tenant, '560001'))
        self.assertEqual(CheckoutSettings.objects.get(tenant=self.tenant).configuration, checkout)

    def test_setup_preserves_hours_that_short_form_cannot_represent(self):
        self.menu()
        checkout, _ = initialize_ordering_settings(self.tenant)
        schedules = ({}, {'0': [['09:00', '12:00'], ['14:00', '18:00']]},
                     {'0': [['09:00', '18:00']], '1': [['10:00', '17:00']]},
                     {str(day): [] for day in range(7)})
        for hours in schedules:
            with self.subTest(hours=hours):
                self.tenant.refresh_from_db()
                self.tenant.meta['ordering_setup_required'] = True
                self.tenant.save()
                checkout.configuration['opening_hours'] = hours
                checkout.save()
                page = self.client.get(self.url)
                self.assertContains(page, 'Your saved opening hours will be preserved')
                self.assertNotContains(page, 'name="setup-opens"')
                # Even stale clients posting the simplified hours cannot overwrite them.
                self.assertEqual(self.client.post(self.url, self.setup_data()).status_code, 302)
                checkout.refresh_from_db()
                self.assertEqual(checkout.configuration['opening_hours'], hours)

    def test_rules_reject_missing_stock_without_enabling_external_integrations(self):
        variant = self.menu()
        _, config = initialize_ordering_settings(self.tenant)
        before = deepcopy(config.policy)
        data = {**ordering_form_data(), 'section': 'ordering', 'stock_policy': 'strict'}
        self.assertContains(self.client.post(self.url, data), 'Configure stock for Coffee', status_code=400)
        config.refresh_from_db()
        self.assertEqual(config.policy, before)
        self.assertFalse(config.local_checkout)
        StockItem.objects.create(location=config.location, item=variant.menu_item, on_hand=100)
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        config.refresh_from_db()
        self.assertTrue(config.local_checkout)
        self.assertFalse(config.enabled)

    def test_integrations_first_save_uses_conservative_model_policy(self):
        self.assertEqual(self.client.post(reverse('commerce:settings'), {}).status_code, 302)
        config = Configuration.objects.get(tenant=self.tenant)
        self.assertEqual(config.policy, Policy().model_dump(mode='json'))
        self.assertIsNone(config.policy['ordering_limits'])
        self.assertEqual(config.policy['stock_policy'], 'strict')
        self.assertFalse(config.local_checkout)
        self.client.get(self.url)
        config.refresh_from_db()
        self.assertIsNone(config.policy['ordering_limits'])

    def test_short_setup_requires_menu_and_confirms_hours_before_enabling_routes(self):
        initialize_ordering_settings(self.tenant)
        publish_default_configuration(self.tenant.pk)
        checkout = CheckoutSettings.objects.get(tenant=self.tenant)
        before = deepcopy(checkout.configuration)
        response = self.client.post(self.url, {**self.setup_data(), 'setup-opens': '10:00'})
        self.assertContains(response, 'Add at least one available menu item', status_code=400)
        checkout.refresh_from_db()
        self.assertEqual(checkout.configuration, before)
        self.menu()
        # Unrelated edits must remain unpublished when setup activates ordering.
        TenantJSONDoc.objects.get_or_create(tenant=self.tenant, dtype='knowledge', intent='information_about_the_cafe',
            sub_intent='location_and_hours', defaults={'payload': {'hours': 'Unpublished draft'}})
        response = self.client.post(self.url, {**self.setup_data(), 'setup-opens': '10:00'})
        self.assertRedirects(response, self.url + '?tab=checkout')
        self.tenant.refresh_from_db(); checkout.refresh_from_db()
        self.assertFalse(self.tenant.meta['ordering_setup_required'])
        self.assertTrue(Configuration.objects.get(tenant=self.tenant).local_checkout)
        self.assertEqual(checkout.configuration['opening_hours']['0'], [['10:00', '18:00']])
        runtime = get_configuration(tenant_id=self.tenant.pk)
        for topic in ('add_to_basket', 'order_confirmation', 'order_payment', 'order_channels_and_modes'):
            self.assertTrue(runtime.allows('placing_order', topic), topic)
        for topic in ('pricing', 'explore_options', 'availability'):
            self.assertTrue(runtime.allows('menu_items', topic), topic)
        self.assertFalse(any(d['payload'] == {'hours': 'Unpublished draft'} for d in runtime.documents))

    def test_delivery_requires_explicit_coverage_and_failed_setup_is_atomic(self):
        initialize_ordering_settings(self.tenant)
        self.menu()
        for data, error in [({'setup-modes': ['delivery']}, 'Enter the postal codes'),
                            ({'setup-closes': '08:00'}, 'Closing time must be later'),
                            ({'setup-confirmed': ''}, 'This field is required')]:
            response = self.client.post(self.url, {**self.setup_data(), **data})
            self.assertContains(response, error, status_code=400)
        self.assertFalse(TenantRuntimeConfiguration.objects.filter(tenant=self.tenant).exists())
        self.assertEqual(CheckoutSettings.objects.get(tenant=self.tenant).configuration, default_checkout_config())
        response = self.client.post(self.url, {**self.setup_data(), 'setup-modes': ['delivery'], 'setup-delivery_postal_codes': '560001'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CheckoutSettings.objects.get(tenant=self.tenant).configuration['delivery_postal_codes'], ['560001'])

    def test_rule_rows_save_money_and_preserve_provider_activation(self):
        self.menu()
        initialize_ordering_settings(self.tenant)
        data = {**ordering_form_data(), 'section': 'ordering', 'packaging': '12.34', 'minimum': '100.50',
            'taxes-TOTAL_FORMS': 1, 'taxes-0-code': 'TAX', 'taxes-0-name': 'Tax', 'taxes-0-rate': '5',
            'discounts-TOTAL_FORMS': 1, 'discounts-0-code': 'SAVE', 'discounts-0-fixed': '10.25',
            'discounts-0-minimum': '200.50', 'discounts-0-modes': ['pickup']}
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        config = Configuration.objects.get(tenant=self.tenant)
        self.assertFalse(config.enabled)
        self.assertEqual(config.policy['packaging_minor'], 1234)
        self.assertEqual(config.policy['minimum_minor'], 10050)
        self.assertEqual(config.policy['discounts'][0]['fixed_minor'], 1025)
        self.assertEqual(config.policy['discounts'][0]['minimum_minor'], 20050)
        self.assertEqual(config.policy['taxes'][0]['rate'], '5')
        self.assertEqual(config.policy['ordering_limits']['max_payable_minor'], 600000)
        response = self.client.get(self.url + '?tab=ordering')
        self.assertContains(response, 'value="12.34"')
        self.assertContains(response, 'value="10.25"')
        data['taxes-0-DELETE'] = 'on'
        data['discounts-0-DELETE'] = 'on'
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.policy['taxes'], [])
        self.assertEqual(config.policy['discounts'], [])

    def test_invalid_rules_are_atomic_and_cannot_reference_other_tenants(self):
        initialize_ordering_settings(self.tenant)
        other = TenantInfo.objects.create(display_name='Other form cafe')
        item = MenuItem.objects.create(tenant=other, name='Other coffee')
        data = {**ordering_form_data(), 'section': 'ordering', 'taxes-TOTAL_FORMS': 1,
            'taxes-0-code': 'TAX', 'taxes-0-name': 'Tax', 'taxes-0-rate': '5', 'taxes-0-item_ids': [str(item.pk)]}
        before = Configuration.objects.get(tenant=self.tenant).policy
        self.assertEqual(self.client.post(self.url, data).status_code, 400)
        self.assertEqual(Configuration.objects.get(tenant=self.tenant).policy, before)
        duplicate = {**data, 'taxes-0-item_ids': [], 'taxes-TOTAL_FORMS': 2,
            'taxes-1-code': 'TAX', 'taxes-1-name': 'Duplicate', 'taxes-1-rate': '5'}
        self.assertContains(self.client.post(self.url, duplicate), 'Rule codes must be unique', status_code=400)
        self.assertEqual(Configuration.objects.get(tenant=self.tenant).policy, before)

    def test_currency_precision_management_data_and_limit_validation(self):
        for changes in ({'currency': 'JPY', 'packaging': '1.50'}, {'max_payable': '4999'},
                        {'discounts-TOTAL_FORMS': 1, 'discounts-0-code': 'BOTH', 'discounts-0-percent': '10', 'discounts-0-fixed': '1'}):
            form = CommerceSettingsForm({**ordering_form_data(), **changes})
            self.assertFalse(form.is_valid(), changes)
        form = CommerceSettingsForm({**ordering_form_data(), 'currency': 'JPY', 'packaging': '15'})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.policy['packaging_minor'], 15)
        self.assertEqual(form.policy['ordering_limits']['max_subtotal_minor'], 5000)
        data = ordering_form_data(); del data['taxes-TOTAL_FORMS']
        self.assertFalse(CommerceSettingsForm(data).is_valid())

    def test_optional_exports_and_shared_delivery_coverage(self):
        checkout, _ = initialize_ordering_settings(self.tenant, demo=True)
        response = self.client.get(self.url + '?download=checkout')
        self.assertEqual(response.json(), checkout.configuration)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertEqual(self.client.get(self.url + '?download=ordering').json(), ordering_defaults(demo=True)['policy'])
        self.menu()
        StockItem.objects.create(location=Configuration.objects.get(tenant=self.tenant).location,
                                 item=MenuItem.objects.get(tenant=self.tenant), on_hand=100)
        complete_ordering_setup(self.tenant, checkout.configuration)
        self.tenant.refresh_from_db()
        from chatbot_core.logic.cafe.db_utils import verify_delivery_pincode
        self.assertTrue(verify_delivery_pincode(self.tenant, '560001'))
        self.assertFalse(verify_delivery_pincode(self.tenant, '110001'))
        self.assertIsNone(verify_delivery_pincode(self.tenant, 'invalid'))
        checkout.configuration['delivery_postal_codes'] = ['110001']
        checkout.save()
        self.assertTrue(verify_delivery_pincode(self.tenant, '110001'))
        self.assertFalse(verify_delivery_pincode(self.tenant, '560001'))

    def test_local_cash_checkout_applies_form_taxes_discounts_and_fees(self):
        variant = self.menu()
        checkout, config = initialize_ordering_settings(self.tenant)
        data = {**ordering_form_data(), 'section': 'ordering', 'packaging': '5',
            'taxes-TOTAL_FORMS': 1, 'taxes-0-code': 'TAX', 'taxes-0-name': 'Tax', 'taxes-0-rate': '10',
            'taxes-0-tax_fees': 'on', 'discounts-TOTAL_FORMS': 1, 'discounts-0-code': 'SAVE', 'discounts-0-percent': '10'}
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='1234567890')
        ChatSession.objects.create(tenant=self.tenant, customer=customer, session_id='rules-order', platform='website',
            state={'checkout': {'mode': 'pickup', 'fields': {'name': 'Guest', 'phone': '1234567890'}, 'discount_code': 'SAVE'}})
        from chatbot_core.logic.cafe.basket import Basket
        from chatbot_core.logic.cafe.checkout import advance_checkout
        basket = Basket(items=[{'item_id': str(variant.menu_item_id), 'item_variant_id': str(variant.pk),
            'name': 'Coffee', 'size': 'Regular', 'quantity': 1, 'unit_price': '100', 'item_number': 1}])
        args = dict(tenant=self.tenant, customer=customer, chat_id='rules-order', platform='website', basket=basket,
                    checklist={}, configuration=checkout.configuration)
        with patch('chatbot_core.logic.cafe.checkout.timezone.now', return_value=datetime(2026, 10, 9, 6, tzinfo=timezone.utc)):
            reply, _, _ = advance_checkout(text='checkout', **args)
            self.assertIn('total: 104.5', reply)
            _, order, _ = advance_checkout(text='confirm', **args)
        self.assertEqual(order.total_amount, Decimal('104.50'))
        self.assertEqual(order.tax_amount, Decimal('9.50'))
        self.assertEqual(order.discount_amount, Decimal('10'))
        self.assertEqual(order.packing_charges, Decimal('5'))
        self.assertFalse(Command.objects.exists())

    def test_demo_accepts_cash_consumes_stock_and_preserves_edits_on_reseed(self):
        with patch.dict(os.environ, DEMO_OWNER_PASSWORD='test-demo-owner-password'):
            call_command('seed_cafe_demo', stdout=io.StringIO())
        tenant = TenantInfo.objects.get(slug='demo-cafe')
        config = Configuration.objects.get(tenant=tenant)
        checkout = CheckoutSettings.objects.get(tenant=tenant)
        self.assertEqual(config.policy, ordering_defaults(demo=True)['policy'])
        self.assertFalse(config.enabled)
        self.assertTrue(get_configuration(tenant_id=tenant.pk).allows('placing_order', 'order_confirmation'))
        availability = next(d['payload'] for d in get_configuration(tenant_id=tenant.pk).documents
                            if d['dtype'] == 'knowledge' and d['intent'] == 'menu_items'
                            and d['sub_intent'] == 'availability')
        for stock in StockItem.objects.filter(location=config.location).select_related('item'):
            self.assertEqual(stock.item.quantity, stock.on_hand)
            self.assertIn({stock.item.name: {'quantity': stock.on_hand}}, availability)
        variant = MenuItemVariant.objects.filter(menu_item__tenant=tenant).first()
        customer = Customer.objects.create(tenant=tenant, name='Guest', phone='1234567890')
        session = ChatSession.objects.create(tenant=tenant, customer=customer, session_id='demo-order', platform='website',
            state={'checkout': {'mode': 'pickup', 'fields': {'name': 'Guest', 'phone': '1234567890'}}})
        from chatbot_core.logic.cafe.basket import Basket
        from chatbot_core.logic.cafe.checkout import advance_checkout
        basket = Basket(items=[{'item_id': str(variant.menu_item_id), 'item_variant_id': str(variant.pk),
            'name': variant.menu_item.name, 'size': variant.size, 'quantity': 1, 'unit_price': str(variant.price), 'item_number': 1}])
        args = dict(tenant=tenant, customer=customer, chat_id=session.session_id, platform='website', basket=basket,
                    checklist={}, configuration=checkout.configuration)
        with patch('chatbot_core.logic.cafe.checkout.timezone.now', return_value=datetime(2026, 10, 9, 6, tzinfo=timezone.utc)):
            reply, _, _ = advance_checkout(text='checkout', **args)
            self.assertIn('Reply confirm', reply)
            _, order, _ = advance_checkout(text='confirm', **args)
        self.assertIsNotNone(order)
        self.assertEqual(order.total_amount, variant.price)
        self.assertEqual(order.commerce_record.state, 'confirmed')
        self.assertEqual(StockItem.objects.get(location=config.location, item=variant.menu_item).on_hand, 99)
        from commerce.knowledge_inventory import inventory_knowledge
        self.assertNotEqual(inventory_knowledge(tenant.pk).get('reason'), 'commerce_disabled')
        self.assertFalse(Command.objects.filter(connection__location=config.location).exists())
        self.assertFalse(ReconciliationIssue.objects.filter(accepted_order__location=config.location).exists())
        checkout.configuration['modes']['pickup']['fee'] = '15'
        checkout.save()
        call_command('seed_cafe_demo', stdout=io.StringIO())
        checkout.refresh_from_db()
        self.assertEqual(checkout.configuration['modes']['pickup']['fee'], '15')
        self.assertEqual(StockItem.objects.get(location=config.location, item=variant.menu_item).on_hand, 99)
