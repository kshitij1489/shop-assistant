from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.contrib.messages import ERROR, get_messages
from django.contrib.messages.storage.base import Message
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.urls import reverse

from chatbot_core.models import TenantInfo, TenantJSONDoc
from commerce.models import Configuration, Connection, Location, StockItem
from orders.models import AddonGroup, ItemAddonGroup, MenuItem, MenuCatalogMeta
from users.models import TenantProfile


class DashboardNotificationTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(display_name='Notification Cafe', approval_status='APPROVED')
        self.user = User.objects.create_user('notification-owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.item = MenuItem.objects.create(tenant=self.tenant, name='Latte')

    def assert_notice(self, response, message, level):
        notices = [(str(notice), notice.level_tag) for notice in get_messages(response.wsgi_request)]
        self.assertEqual(notices, [(message, level)])
        self.assertContains(response, f'data-notification-level="{level}"', status_code=response.status_code)
        self.assertNotContains(response, 'class="messages"', status_code=response.status_code)

    def test_knowledge_changes_report_one_confirmed_result(self):
        url = reverse('tenant:tenant_knowledge')
        data = {'dtype': 'knowledge', 'intent': 'general', 'sub_intent': 'hours', 'payload': '{}'}
        for action, message, level in (
            ('add', 'Knowledge Draft Saved', 'success'),
            ('add', 'Entry Exists: Use Edit / Save', 'warning'),
            ('update', 'Knowledge Draft Saved', 'success'),
            ('delete', 'Knowledge Draft Deleted', 'success'),
            ('delete', 'Knowledge Entry Not Found', 'warning'),
        ):
            with self.subTest(action=action, message=message):
                response = self.client.post(url, {**data, 'action': action}, follow=True)
                self.assert_notice(response, message, level)

    def test_failed_knowledge_save_never_announces_saved_draft(self):
        response = self.client.post(reverse('tenant:tenant_knowledge'), {
            'action': 'add', 'dtype': 'knowledge', 'intent': 'general', 'sub_intent': 'hours', 'payload': '{invalid',
        }, follow=True)
        self.assert_notice(response, 'Invalid JSON', 'error')
        self.assertContains(response, 'Validation details')
        self.assertContains(response, 'Expecting property name enclosed in double quotes')
        self.assertFalse(TenantJSONDoc.objects.filter(tenant=self.tenant).exists())

    def test_server_notices_are_visible_without_javascript(self):
        html = render_to_string('users/partials/notifications.html', {
            'messages': [Message(ERROR, 'Failure')],
        })
        self.assertNotIn('hidden', html)
        self.assertIn('role="alert"', html)
        self.assertIn('data-notification-level="error"', html)
        self.assertIn('Failure', html)

    @override_settings(PUBLIC_URL='https://example.test')
    def test_telegram_failure_details_never_fall_back_to_raw_body(self):
        for payload in ({}, [], None, {'description': 'Invalid webhook URL'}):
            with self.subTest(payload=payload):
                response = Mock(ok=False, text='https://telegram.test/botsecret/setWebhook' * 1000)
                response.json.return_value = payload
                if payload is None:
                    response.json.side_effect = ValueError('Not JSON')
                with patch('users.views.requests.post', return_value=response):
                    with self.captureOnCommitCallbacks(execute=True):
                        result = self.client.post(reverse('tenant:tenant_settings'), {
                            'section': 'integrations', 'telegram_bot_token': 'secret',
                        })
                # Test callbacks run after session middleware, so inspect the request session.
                expected = payload.get('description') if isinstance(payload, dict) else None
                self.assertEqual(result.wsgi_request.session['action_error_details'],
                                 [expected or 'Telegram could not register the webhook.'])

    def test_publication_error_is_brief_with_escaped_details_consumed_once(self):
        detail = 'Missing topic <script>unsafe</script>'
        with patch('users.views.publish_configuration', side_effect=ValidationError([detail])):
            response = self.client.post(reverse('tenant:tenant_knowledge'), {
                'action': 'publish', 'version': 0,
            }, follow=True)
        self.assert_notice(response, 'Configuration Not Published', 'error')
        self.assertContains(response, 'Missing topic &lt;script&gt;unsafe&lt;/script&gt;')
        self.assertNotContains(response, '<script>unsafe</script>')
        self.assertNotContains(self.client.get(reverse('tenant:tenant_knowledge')), 'Missing topic &lt;script&gt;unsafe&lt;/script&gt;')
        self.assertNotIn('action_error_details', self.client.session)

    def test_malformed_catalog_json_preserves_existing_metadata(self):
        metadata = MenuCatalogMeta.objects.create(menu_item=self.item, nutrition={'calories': 50}, ingredients=['coffee'])
        response = self.client.post(reverse('tenant:tenant_menu_item_catalog_update', args=[self.item.pk]), {
            'nutrition': '{invalid', 'ingredients': '["milk"]',
        }, follow=True)
        self.assert_notice(response, 'Catalog Meta Not Saved', 'error')
        metadata.refresh_from_db()
        self.assertEqual(metadata.nutrition, {'calories': 50})
        self.assertEqual(metadata.ingredients, ['coffee'])
        self.assertContains(response, 'Nutrition must be valid JSON.')

    def test_valid_catalog_save_reports_success_and_retains_plain_preparation(self):
        response = self.client.post(reverse('tenant:tenant_menu_item_catalog_update', args=[self.item.pk]), {
            'nutrition': '{"calories":50}', 'preparation': 'Serve hot',
        }, follow=True)
        self.assert_notice(response, 'Catalog Meta Saved', 'success')
        self.assertEqual(self.item.catalog_meta.preparation, {'text': 'Serve hot'})

    def test_menu_import_reports_short_success_or_failure(self):
        url = reverse('tenant:tenant_menu_ingest_json')
        with patch('orders.catalog_imports.import_catalog', return_value=(1, 2, 3, 4)):
            self.assert_notice(self.client.post(url, {'menu_items_json': '{}'}, follow=True), 'Menu Items Saved', 'success')
        response = self.client.post(url, {'menu_items_json': '{invalid'}, follow=True)
        self.assert_notice(response, 'Menu Import Failed', 'error')

    def test_modifier_saves_detaches_and_deletes_have_notifications(self):
        response = self.client.post(reverse('tenant:modifiers'), {
            'name': 'Milk', 'addons-TOTAL_FORMS': 0, 'addons-INITIAL_FORMS': 0,
        }, follow=True)
        self.assert_notice(response, 'Modifier Group Saved', 'success')
        group = AddonGroup.objects.get(tenant=self.tenant)
        response = self.client.post(reverse('tenant:item_modifiers', args=[self.item.pk]), {
            'group': group.pk, 'min_selections': 0, 'max_selections': 1,
        }, follow=True)
        self.assert_notice(response, 'Item Rules Saved', 'success')
        link = ItemAddonGroup.objects.get(item=self.item)
        response = self.client.post(reverse('tenant:item_modifier_edit', args=[self.item.pk, link.pk]), {
            'action': 'delete',
        }, follow=True)
        self.assert_notice(response, 'Modifier Group Detached', 'success')
        response = self.client.post(reverse('tenant:modifier_edit', args=[group.pk]), {'action': 'delete'}, follow=True)
        self.assert_notice(response, 'Modifier Group Deleted', 'success')

    def test_connection_create_update_rotate_and_stock_results(self):
        location = Location.objects.create(tenant=self.tenant, code='main', name='Main')
        Configuration.objects.create(tenant=self.tenant, location=location)
        data = {'provider': 'custom', 'role': 'pos', 'account_id': 'cafe', 'environment': 'test',
            'capabilities': ['order.submit', 'order.reconcile'], 'active': 'on'}
        response = self.client.post(reverse('commerce:connections'), data, follow=True)
        self.assert_notice(response, 'Connection Saved', 'success')
        connection = Connection.objects.get(location=location)
        url = reverse('commerce:connection_edit', args=[connection.pk])
        self.assert_notice(self.client.post(url, data, follow=True), 'Connection Saved', 'success')
        self.assert_notice(self.client.post(url, {'action': 'rotate'}, follow=True), 'Signing Secret Rotated', 'success')
        stock_data = {'item': self.item.pk, 'mode': 'quantity', 'on_hand': 5, 'available': 'on'}
        self.assert_notice(self.client.post(reverse('commerce:stock'), stock_data, follow=True), 'Stock Saved', 'success')
        stock = StockItem.objects.get(location=location)
        StockItem.objects.filter(pk=stock.pk).update(reserved=3)
        response = self.client.post(reverse('commerce:stock_edit', args=[stock.pk]), {**stock_data, 'on_hand': 2})
        self.assert_notice(response, 'Stock Not Saved', 'error')
