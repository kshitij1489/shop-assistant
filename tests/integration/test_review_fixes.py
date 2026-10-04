"""Regressions for generalized integration cleanup and checkout review."""
import json
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase, RequestFactory, override_settings
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.urls import reverse
from chatbot_core.channels import website
from chatbot_core.channels.utils import generate_tenant_jwt
from chatbot_core.logic.cafe.checkout import quote, advance_checkout
from chatbot_core.logic.cafe.order_interpreter import interpretation_context
from orders.models import CheckoutSettings, ChatSession, Order, Tax, VariantTaxMap
from orders.checkout_config import default_checkout_config, CheckoutPolicy
from commerce.models import AcceptedOrder, Payment, Command, Connection
from commerce.services import accept_order, basket_quote
from commerce.credentials import adapter_secret
from commerce.payment_links import initiate_payment
from users.models import TenantProfile
from users.analytics.db_utils import execute_db_query
from tests.support.commerce import Fixtures


@override_settings(COMMERCE_ADAPTER_SECRETS={'test-key': 'test-adapter-secret'}, ROOT_URLCONF='tests.support.urls')
class ReviewFixTests(Fixtures, TestCase):
    def setUp(self):
        self.seed()
        self.policy = default_checkout_config()
        self.policy['modes'] = {'pickup': {'required_fields': [], 'payment_methods': ['cash'], 'fee': '2'}}
        self.draft = {'mode': 'pickup', 'fields': {}}

    def test_delivery_defaults_collect_contact_details(self):
        self.assertEqual(default_checkout_config()['modes']['delivery']['required_fields'], ['name', 'phone', 'address'])

    def test_online_publication_requires_credentials_capabilities_and_enabled_commerce(self):
        self.policy['online_provider'] = 'adapter'
        self.policy['modes']['pickup']['payment_methods'] = ['online']
        row = CheckoutSettings(tenant=self.tenant, configuration=self.policy)
        row.save()
        for field, value in [('active', False), ('capabilities', ['payment.create']), ('secret_ref', 'missing')]:
            old = deepcopy(getattr(self.gateway, field))
            setattr(self.gateway, field, value)
            self.gateway.save()
            with self.subTest(field=field), self.assertRaises(ValidationError):
                row.save()
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                quote(CheckoutPolicy.model_validate(self.policy), self.draft, self.basket, self.tenant)
            setattr(self.gateway, field, old)
            self.gateway.save()
        self.config.enabled = False
        self.config.save()
        with self.assertRaises(ValidationError):
            row.save()

    def test_retired_simulator_is_not_a_valid_provider(self):
        self.policy['online_provider'] = 'dummy'
        with self.assertRaises(ValueError):
            CheckoutPolicy.model_validate(self.policy)

    def test_fee_codes_drive_quote_and_persisted_charges_once_for_each_mode(self):
        self.config.policy['packaging_minor'] = 100
        self.config.save()
        for mode, charge in [('pickup', 'packing_charges'), ('dine_in', 'service_charge'), ('delivery', 'delivery_charges')]:
            policy = default_checkout_config()
            policy['modes'] = {mode: {'required_fields': ['address'] if mode == 'delivery' else [], 'fee': '2'}}
            session = ChatSession.objects.create(tenant=self.tenant, customer=self.customer, session_id=mode, platform='website')
            fields = {'address': '42 Main Street'} if mode == 'delivery' else {}
            session.state = {'checkout': {'mode': mode, 'fields': fields}}
            session.save()
            self.stock.on_hand = 10
            self.stock.save()
            args = dict(tenant=self.tenant, customer=self.customer, chat_id=mode, platform='website', basket=self.basket, checklist={}, configuration=policy)
            reply, _, _ = advance_checkout(text='checkout', **args)
            self.assertIn('fee: 3', reply)
            _, order, _ = advance_checkout(text='confirm', **args)
            self.assertEqual(order.total_amount, Decimal('13.05'))
            self.assertEqual(order.packing_charges, 3 if mode == 'pickup' else 1)
            self.assertEqual(getattr(order, charge), 3 if mode == 'pickup' else 2)
            self.assertEqual(order.delivery_charges + order.packing_charges + order.service_charge, 3)

    def test_noncommerce_tax_snapshot_and_requote_after_tax_change(self):
        self.config.enabled = False
        self.config.save()
        tax = Tax.objects.create(tenant=self.tenant, title='VAT', type='P', rate_display='10%')
        fixed = Tax.objects.create(tenant=self.tenant, title='Fixed', type='F', rate_display='1.25')
        for rule in (tax, fixed):
            VariantTaxMap.objects.create(variant=self.variant, tax=rule)
        self.basket.items[0]['quantity'] = 2
        session = ChatSession.objects.create(tenant=self.tenant, customer=self.customer, session_id='tax', platform='website')
        args = dict(tenant=self.tenant, customer=self.customer, chat_id='tax', platform='website', basket=self.basket, checklist={}, configuration=self.policy)
        reply, _, _ = advance_checkout(text='checkout', **args)
        self.assertIn('Tax: 4.51', reply)
        tax.rate_display = '20%'
        tax.save()
        reply, order, _ = advance_checkout(text='confirm', **args)
        self.assertIsNone(order)
        self.assertIn('Tax: 6.52', reply)
        _, order, _ = advance_checkout(text='confirm', **args)
        self.assertEqual(order.total_amount, Decimal('28.62'))
        self.assertEqual(order.tax_amount, Decimal('6.52'))
        self.assertEqual(sum(Decimal(t['amount']) for t in order.items.get().item_tax_snapshot), order.tax_amount)

    def test_zero_online_total_rolls_back_reservations_and_pos_commands(self):
        self.variant.price = 0
        self.variant.save()
        self.basket.items[0]['unit_price'] = '0'
        pricing = basket_quote(self.tenant, self.basket, mode='pickup')
        order = Order.objects.create(tenant=self.tenant, customer=self.customer, total_amount=0, payment_mode='online')
        with self.assertRaisesRegex(ValueError, 'positive total'):
            accept_order(order, pricing)
        self.stock.refresh_from_db()
        self.assertEqual(self.stock.reserved, 0)
        self.assertFalse(AcceptedOrder.objects.exists())
        self.assertFalse(Command.objects.exists())
        order.payment_mode = 'cash'
        order.save()
        self.assertEqual(accept_order(order, pricing).state, 'confirmed')
        self.assertFalse(Payment.objects.exists())

    def test_payment_link_is_only_read_from_accepted_adapter_payment(self):
        orphan = Order.objects.create(tenant=self.tenant, total_amount=1, payment_mode='online')
        with self.assertRaises(ValueError):
            initiate_payment(orphan)
        record = self.accept()
        self.assertTrue(initiate_payment(record.order)['pending'])
        payment = record.payments.get()
        payment.checkout_url = 'https://provider.example/pay'
        payment.save()
        self.assertEqual(initiate_payment(record.order)['payment_url'], payment.checkout_url)
        self.assertEqual(Command.objects.filter(kind='payment.create').count(), 1)

    def test_analytics_masks_checkout_pii_before_aliases(self):
        Order.objects.create(tenant=self.tenant, total_amount=1, meta={'checkout': {'name': 'Private', 'phone': '123'}})
        ChatSession.objects.create(tenant=self.tenant, customer=self.customer, session_id='private', platform='website', state={'address': 'Private'})
        for table, column in [('orders_order', 'meta'), ('orders_chatsession', 'state')]:
            result = execute_db_query({'sql': f'SELECT {column} AS leaked FROM {table}', 'params': []}, tenant_id=self.tenant.pk)
            self.assertEqual(result['rows'], [{'leaked': None}])

    def test_inactive_tenant_cannot_enter_dashboard_and_gets_do_not_lock(self):
        user = get_user_model().objects.create_user('review-owner')
        TenantProfile.objects.create(user=user, tenant=self.tenant)
        self.client.force_login(user)
        with patch('django.db.models.query.QuerySet.select_for_update', side_effect=AssertionError('GET acquired row lock')):
            self.assertEqual(self.client.get(reverse('commerce:connections')).status_code, 200)
            self.assertEqual(self.client.get(reverse('commerce:stock_edit', args=[self.stock.pk])).status_code, 200)
        self.tenant.is_active = False
        self.tenant.save()
        response = self.client.get(reverse('commerce:settings'))
        self.assertEqual((response.status_code, response.url), (302, reverse('pending_review')))
        self.assertEqual(self.client.get(reverse('commerce:settings'), HTTP_ACCEPT='application/json').status_code, 403)

    @override_settings(JWT_SECRET='review-website-secret-with-at-least-32-characters')
    def test_disabled_or_unapproved_tenant_cannot_mint_or_use_website_token(self):
        factory = RequestFactory()
        token = generate_tenant_jwt(self.tenant.slug)
        for active, status in [(False, 'APPROVED'), (True, 'PENDING')]:
            self.tenant.is_active, self.tenant.approval_status = active, status
            self.tenant.save()
            request = factory.get('/token/', {'tenant': self.tenant.slug}, HTTP_X_API_KEY=self.tenant.api_key)
            self.assertEqual(website.public_jwt_token(request).status_code, 404)
            request = factory.post('/chatbot-api/', {'message': 'hi'}, HTTP_AUTHORIZATION='Bearer '+token)
            self.assertEqual(website.chatbot_api(request).status_code, 404)

    def test_llm_context_has_no_prices_at_any_depth(self):
        data = {'catalog': [{'variants': [{'id': 'v', 'price': '10'}], 'modifier_groups': [{'options': [{'id': 'a', 'price': '5'}]}]}],
                'basket': [{'unit_price': '10', 'modifiers': [{'unit_price': '5'}]}], 'pending': {'total_price': '15'}}
        projected = interpretation_context(data)
        self.assertNotIn('price', json.dumps(projected))
        self.assertEqual(projected['catalog'][0]['modifier_groups'][0]['options'], [{'id': 'a'}])
        self.assertIn('price', json.dumps(data))

    def test_reconciliation_task_has_stable_name_and_late_ack(self):
        from commerce.tasks import reconcile_commerce
        self.assertEqual(reconcile_commerce.name, 'commerce.tasks.reconcile_commerce')
        self.assertTrue(reconcile_commerce.acks_late)

    def test_migration_registers_unique_secrets_and_disables_all_shared_credentials(self):
        import importlib
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        from commerce.models import Location
        location = Location.objects.create(tenant=self.tenant, code='upgrade', name='Upgrade')
        # Bulk insertion represents old rows before fingerprints existed.
        old = [Connection(location=location, provider='old', role=role, account_id=role,
                          active=True, secret_ref='shared') for role in ('pos', 'payment')]
        Connection.objects.bulk_create(old)
        migration = importlib.import_module('commerce.migrations.0004_register_adapter_credentials')
        with override_settings(COMMERCE_ADAPTER_SECRETS={'test-key': 'test-adapter-secret', 'shared': 'duplicate'}):
            migration.register(apps, SimpleNamespace(connection=connection))
        for row in old:
            row.refresh_from_db()
            self.assertFalse(row.active)
            self.assertIsNone(row.secret_fingerprint)
        self.gateway.refresh_from_db()
        self.assertTrue(self.gateway.active)
        self.assertIsNotNone(adapter_secret(self.gateway))

    def test_migration_retires_simulator_settings_without_rewriting_orders(self):
        import importlib
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        self.policy['online_provider'] = 'dummy'
        self.policy['modes']['pickup']['payment_methods'] = ['online']
        CheckoutSettings.objects.bulk_create([CheckoutSettings(tenant=self.tenant, configuration=self.policy)])
        order = Order.objects.create(tenant=self.tenant, total_amount=1, payment_mode='online')
        migration = importlib.import_module('orders.migrations.0020_retire_simulator_checkout')
        migration.retire_simulator(apps, SimpleNamespace(connection=connection))
        row = CheckoutSettings.objects.get(tenant=self.tenant)
        self.assertEqual(row.configuration['online_provider'], '')
        self.assertEqual(row.configuration['modes']['pickup']['payment_methods'], ['cash'])
        order.refresh_from_db()
        self.assertEqual(order.payment_mode, 'online')

    def test_legacy_basket_merge_refreshes_current_price(self):
        self.basket.items[0]['unit_price'] = '9.00'
        with patch('chatbot_core.logic.cafe.basket.search_cache', return_value={
                'item_id': str(self.item.pk), 'item_variant_id': str(self.variant.pk), 'unit_price': '10.05'}):
            self.assertTrue(self.basket.add_item('Coffee', self.tenant.api_key, 'Regular'))
        self.assertEqual(len(self.basket.items), 1)
        self.assertEqual(self.basket.items[0]['quantity'], 2)
        self.assertEqual(self.basket.items[0]['unit_price'], '10.05')
