"""Unavailable settings cannot authorize unsupported checkout or channel actions."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse

from chatbot_core.channels import whatsapp
from chatbot_core.configuration_imports import import_checkout
from chatbot_core.models import TenantInfo
from orders.checkout_config import ModePolicy, default_checkout_config
from orders.models import CheckoutSettings, Order
from tests.support.checkout import CheckoutFixture
from users.models import TenantProfile


class Controls(HTMLParser):
    def __init__(self, response):
        super().__init__()
        self.nodes = []
        self.feed(response.content.decode())

    def handle_starttag(self, tag, attrs):
        if tag in {'input', 'textarea', 'select', 'button'}:
            self.nodes.append((tag, dict(attrs)))

    def field(self, name, value=None):
        return next(attrs for tag, attrs in self.nodes if attrs.get('name') == name
                    and (value is None or attrs.get('value') == value))


class SettingsAvailabilityTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Availability cafe', approval_status='APPROVED')
        self.user = get_user_model().objects.create_user('availability-owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.url = reverse('tenant:tenant_settings')

    def checkout_data(self, mode='pickup'):
        return {'section': 'checkout', 'modes': [mode], 'timezone': 'Asia/Kolkata',
                'always_open': 'on', f'{mode}_payment_methods': ['cash'],
                f'{mode}_required_fields': ['address'] if mode == 'delivery' else ['name', 'phone']}

    def test_saved_scheduling_is_shown_off_with_disabled_controls(self):
        config = default_checkout_config()
        config['modes']['pickup'].update(scheduling_enabled=True, required_fields=['name', 'scheduled_at'])
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=config)
        response = self.client.get(self.url)
        controls = Controls(response)
        for mode in ('delivery', 'pickup', 'dine_in'):
            self.assertIn('disabled', controls.field(f'{mode}_scheduling_enabled'))
            self.assertNotIn('checked', controls.field(f'{mode}_scheduling_enabled'))
            self.assertIn('disabled', controls.field(f'{mode}_max_advance_days'))
            self.assertContains(response, 'allow scheduling (Coming soon)')
        pickup_time = controls.field('pickup_required_fields', 'scheduled_at')
        self.assertIn('disabled', pickup_time)
        self.assertNotIn('checked', pickup_time)
        self.assertNotIn('disabled', controls.field('pickup_preparation_minutes'))
        self.assertNotIn('disabled', controls.field('hours_0'))

    def test_forged_schedule_switch_is_ignored_and_required_pickup_time_is_rejected(self):
        data = {**self.checkout_data(), 'pickup_scheduling_enabled': 'on', 'pickup_max_advance_days': 365}
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        saved = CheckoutSettings.objects.get(tenant=self.tenant).configuration
        self.assertFalse(saved['modes']['pickup']['scheduling_enabled'])
        self.assertEqual(saved['modes']['pickup']['max_advance_days'], 7)
        response = self.client.post(self.url, {**data, 'pickup_required_fields': ['scheduled_at']})
        self.assertEqual(response.status_code, 400)
        self.assertIn('pickup_required_fields', response.context['checkout_form'].errors)
        self.assertContains(response, 'Scheduled pickup time is coming soon', status_code=400)
        self.assertEqual(CheckoutSettings.objects.get(tenant=self.tenant).configuration, saved)

    def test_saving_legacy_schedule_keeps_contact_fields_and_clears_scheduling(self):
        config = default_checkout_config()
        config['modes']['pickup'].update(scheduling_enabled=True, required_fields=['name', 'scheduled_at'])
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=config)
        self.assertEqual(self.client.post(self.url, self.checkout_data()).status_code, 302)
        saved = CheckoutSettings.objects.get(tenant=self.tenant).configuration['modes']['pickup']
        self.assertEqual(saved['required_fields'], ['name', 'phone'])
        self.assertFalse(saved['scheduling_enabled'])

    def test_imports_reject_unavailable_settings_without_replacing_saved_policy(self):
        saved = default_checkout_config()
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=saved)
        for required_time in (False, True):
            config = deepcopy(saved)
            config['modes']['pickup']['scheduling_enabled'] = True
            if required_time:
                config['modes']['pickup']['required_fields'].append('scheduled_at')
            with self.assertRaisesMessage(ValidationError, 'Order scheduling is coming soon'):
                import_checkout(self.tenant, config)
        config = deepcopy(saved)
        config['modes']['delivery'] = ModePolicy(required_fields=['address']).model_dump(mode='json')
        config['delivery_postal_codes'] = ['SW1A 1AA']
        with self.assertRaisesMessage(ValidationError, 'International delivery is coming soon'):
            import_checkout(self.tenant, config)
        self.assertEqual(CheckoutSettings.objects.get(tenant=self.tenant).configuration, saved)

    def test_delivery_accepts_indian_pincodes_and_rejects_international_codes(self):
        data = self.checkout_data('delivery')
        for code in ('SW1A 1AA', '90210', '012345', '１１０００１'):
            with self.subTest(code=code):
                response = self.client.post(self.url, {**data, 'delivery_postal_codes': code})
                self.assertEqual(response.status_code, 400)
                self.assertIn('delivery_postal_codes', response.context['checkout_form'].errors)
        response = self.client.post(self.url, {**data, 'delivery_postal_codes': '110001, 560001'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(CheckoutSettings.objects.get(tenant=self.tenant).configuration['delivery_postal_codes'],
                         ['110001', '560001'])
        page = self.client.get(self.url + '?tab=checkout&subtab=delivery')
        self.assertContains(page, 'disabled aria-describedby="international-delivery-help"')
        self.assertNotIn('disabled', Controls(page).field('delivery_postal_codes'))

    def test_setup_cannot_enable_international_delivery(self):
        response = self.client.post(self.url, {'section': 'ordering_setup', 'setup-modes': ['delivery'],
            'setup-timezone': 'Asia/Kolkata', 'setup-days': ['0'], 'setup-opens': '09:00',
            'setup-closes': '18:00', 'setup-confirmed': 'on', 'setup-delivery_postal_codes': 'SW1A 1AA'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('delivery_postal_codes', response.context['ordering_setup_form'].errors)

    @override_settings(PUBLIC_URL='https://example.test')
    def test_telegram_connects_and_reregisters_but_change_and_disconnect_are_disabled(self):
        with patch('users.views.requests.post', return_value=Mock(ok=True, json=lambda: {'ok': True})) as provider:
            for _ in range(2):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(self.url, {'section': 'integrations', 'telegram_bot_token': '123:audit'})
                self.assertEqual(response.status_code, 302)
            self.assertEqual(provider.call_count, 2)
            for token in ('', '123:different'):
                response = self.client.post(self.url, {'section': 'integrations', 'telegram_bot_token': token})
                self.assertEqual(response.status_code, 400)
                self.assertIn('telegram_bot_token', response.context['telegram_form'].errors)
            self.assertEqual(provider.call_count, 2)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.telegram_bot_token, '123:audit')
        page = self.client.get(self.url + '?tab=integrations')
        self.assertIn('readonly', Controls(page).field('telegram_bot_token'))
        self.assertContains(page, 'disabled>Change or disconnect Telegram (Coming soon)')

    def test_empty_and_duplicate_telegram_tokens_do_not_cause_server_errors(self):
        for _ in range(2):
            TenantInfo.objects.create(display_name='Other business', telegram_bot_token=None)
            response = self.client.post(self.url, {'section': 'integrations', 'telegram_bot_token': ''})
            self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.telegram_bot_token)
        TenantInfo.objects.create(display_name='Connected business', telegram_bot_token='123:connected')
        for token in ('123:connected', 'x' * 201):
            response = self.client.post(self.url, {'section': 'integrations', 'telegram_bot_token': token})
            self.assertEqual(response.status_code, 400)
            self.assertIn('telegram_bot_token', response.context['telegram_form'].errors)
        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.telegram_bot_token)

    def test_telegram_connection_race_returns_a_form_error(self):
        self.client.get(self.url)
        with patch.object(TenantInfo, 'save', side_effect=IntegrityError('Concurrent connection')):
            response = self.client.post(self.url, {'section': 'integrations', 'telegram_bot_token': '123:new'})
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, 'already connected to another business', status_code=400)
        self.tenant.refresh_from_db()
        self.assertIsNone(self.tenant.telegram_bot_token)

    def test_whatsapp_contact_number_works_while_chatbot_connection_is_disabled(self):
        response = self.client.post(self.url, {'section': 'whatsapp', 'whatsapp_number': '+919876543210'})
        self.assertEqual(response.status_code, 302)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.whatsapp_number, '+919876543210')
        self.assertIsNone(self.tenant.whatsapp_id)
        page = self.client.get(self.url + '?tab=integrations')
        self.assertContains(page, 'disabled>Connect WhatsApp (Coming soon)')
        self.assertFalse(whatsapp.WHATSAPP_CHATBOT_AVAILABLE)


class ExistingScheduleCheckoutTests(CheckoutFixture, TestCase):
    def test_old_unconfirmed_schedule_does_not_require_or_retain_an_unavailable_pickup_time(self):
        self.config['modes']['pickup'].update(scheduling_enabled=True, required_fields=['scheduled_at'])
        CheckoutSettings.objects.create(tenant=self.tenant, configuration=self.config)
        self.turn('pickup')
        future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
        self.turn(future)
        self.session.refresh_from_db()
        self.assertIn('scheduled_at', self.session.state['checkout']['fields'])
        store = self.graph_store()
        reply, _ = self.graph_turn(store, 'checkout')
        self.assertNotIn('Scheduled:', reply)
        self.assertNotIn('scheduled_at', store.get_checklist()['checkout']['fields'])
        self.assertFalse(Order.objects.exists())
        self.graph_turn(store, 'confirm')
        self.assertEqual(Order.objects.get().advanced_order, 'N')
