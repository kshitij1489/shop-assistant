"""Adversarial checks at legacy API, export, worker, SQL and admin boundaries."""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory, Client, override_settings
from chatbot_core.models import TenantInfo
from orders.models import Customer as CoreCustomer, Order as CoreOrder, MenuItem, MenuItemVariant
from users.models import TenantProfile
from users.analytics.db_utils import execute_db_query, ExecutionError


class TenantFixture(TestCase):
    def setUp(self):
        self.a = TenantInfo.objects.create(display_name='A', slug='tenant-a', approval_status='APPROVED')
        self.b = TenantInfo.objects.create(display_name='B', slug='tenant-b', approval_status='APPROVED')
        self.user = get_user_model().objects.create_user('tenant-a')
        TenantProfile.objects.create(user=self.user, tenant=self.a)
        self.client.force_login(self.user)


class TenantBoundaryTests(TenantFixture):
    def test_voice_rejects_anonymous_and_foreign_tenant_before_enqueue(self):
        body = {'tenant_id': self.b.pk, 'message': {'text': 'hello'}}
        with patch('chatbot_core.channels.voice_assistant.enqueue_user_message') as enqueue:
            self.assertEqual(Client().post('/voice/', body, content_type='application/json').status_code, 403)
            self.assertEqual(self.client.post('/voice/', body, content_type='application/json').status_code, 403)
            enqueue.assert_not_called()
            body['tenant_id'] = self.a.pk
            response = self.client.post('/voice/', body, content_type='application/json')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(enqueue.call_args.args[0], str(self.a.pk))
            self.assertEqual(enqueue.call_args.args[2]['user_id'], response.json()['chat_id'])
            body['chat_id'] = 'foreign-chat'
            self.assertEqual(self.client.post('/voice/', body, content_type='application/json').status_code, 403)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post('/voice/', body, content_type='application/json').status_code, 403)

    def test_django_admin_denies_tenant_staff_even_with_model_permissions(self):
        self.user.is_staff = True
        self.user.save()
        self.assertEqual(self.client.get('/admin/orders/order/').status_code, 302)
        from django.contrib import admin
        request = RequestFactory().get('/admin/')
        request.user = self.user
        self.assertFalse(admin.site.has_permission(request))
        self.user.is_superuser = True
        self.assertTrue(admin.site.has_permission(request))

    def test_shared_commerce_secret_is_rejected_at_write_and_changed_secrets_fail_closed(self):
        import time
        from django.db import IntegrityError, transaction
        from commerce.models import Connection, Location
        from commerce.api import manifest, signature
        first_location = Location.objects.create(tenant=self.a, code='a', name='A')
        second_location = Location.objects.create(tenant=self.b, code='b', name='B')
        with override_settings(COMMERCE_ADAPTER_SECRETS={'first': 'same-secret', 'second': 'same-secret'}):
            first = Connection.objects.create(location=first_location,
                provider='test', role='pos', active=True, account_id='a', secret_ref='first')
            with self.assertRaises(IntegrityError), transaction.atomic():
                Connection.objects.create(location=second_location,
                    provider='test', role='pos', active=True, account_id='b', secret_ref='second')
        stamp = str(int(time.time()))
        path = f'/connections/{first.pk}/manifest/'
        request = RequestFactory().get(path, HTTP_X_COMMERCE_TIMESTAMP=stamp,
            HTTP_X_COMMERCE_SIGNATURE=signature('same-secret', stamp, 'GET', path, b''))
        with override_settings(COMMERCE_ADAPTER_SECRETS={'first': 'same-secret'}):
            self.assertEqual(manifest(request, first.pk).status_code, 200)
        with override_settings(COMMERCE_ADAPTER_SECRETS={'first': 'changed-secret'}):
            self.assertEqual(manifest(request, first.pk).status_code, 401)

    def test_queue_rejects_mismatched_tenant_user_and_channel(self):
        from chatbot_core.tasks import enqueue_user_message, validate_payload_scope, _qkey
        payload = {'tenant_id': str(self.a.pk), 'user_id': 'same-user', 'channel': 'telegram'}
        with patch('chatbot_core.tasks._r') as redis:
            with self.assertRaises(ValueError):
                enqueue_user_message(self.b.pk, 'same-user', payload)
            with self.assertRaises(ValueError):
                enqueue_user_message(self.a.pk, 'foreign-user', payload)
            with self.assertRaises(ValueError):
                validate_payload_scope(self.a.pk, 'same-user', payload, 'website')
            redis.assert_not_called()
        self.assertNotEqual(_qkey(self.a.pk, 'same-user', 'telegram'), _qkey(self.b.pk, 'same-user', 'telegram'))
        self.assertNotEqual(_qkey(self.a.pk, 'same-user', 'telegram'), _qkey(self.a.pk, 'same-user', 'website'))

    def test_worker_revalidates_queued_payload_before_processing(self):
        from chatbot_core.tasks import drain_user_queue_task
        payload = {'tenant_id': str(self.b.pk), 'user_id': 'same-user', 'channel': 'telegram'}
        with patch('chatbot_core.tasks._r') as redis, patch('chatbot_core.processor.process_payload') as process:
            redis.return_value.lpop.side_effect = [json.dumps(payload), None]
            with self.assertRaises(ValueError):
                drain_user_queue_task.run(self.a.pk, 'same-user', 'telegram')
            process.assert_not_called()

    def test_processor_checks_bot_credential_after_channel_normalization(self):
        from chatbot_core.processor import process_payload
        self.a.telegram_bot_token = 'tenant-a-token'
        self.a.save(update_fields=['telegram_bot_token'])
        for channel in ('telegram', 'Telegram', ' telegram '):
            payload = {'tenant_id': str(self.a.pk), 'user_id': 'user', 'channel': channel, 'bot_token': 'foreign-token'}
            with self.subTest(channel=channel), patch('chatbot_core.processor.get_adapter') as adapter:
                with self.assertRaises(ValueError):
                    process_payload(self.a.pk, 'user', payload)
                adapter.assert_not_called()

    def test_unsigned_whatsapp_cannot_choose_a_tenant(self):
        from chatbot_core.channels.whatsapp import whatsapp_webhook
        request = RequestFactory().post('/', {'entry': []}, content_type='application/json')
        with override_settings(WHATSAPP_APP_SECRET='configured'):
            with patch('chatbot_core.channels.whatsapp.route_message_for_tenant') as route:
                self.assertEqual(whatsapp_webhook(request).status_code, 403)
                route.assert_not_called()


class AnalyticsBoundaryTests(TestCase):
    def setUp(self):
        self.a = TenantInfo.objects.create(display_name='A')
        self.b = TenantInfo.objects.create(display_name='B')
        self.ca = CoreCustomer.objects.create(tenant=self.a, name='Alice', phone='a-secret')
        self.cb = CoreCustomer.objects.create(tenant=self.b, name='Bob', phone='b-secret')

    def query(self, sql, params=None, **kwargs):
        return execute_db_query({'sql': sql, 'params': params or [], 'columns': ['spoofed'], 'safety': {'is_safe': True}}, self.a.pk, **kwargs)

    def test_scoped_rows_aggregates_or_filters_and_masking_before_alias(self):
        self.assertEqual(self.query('SELECT name FROM orders_customer')['rows'], [{'name': 'Alice'}])
        self.assertEqual(self.query('SELECT COUNT(*) AS total FROM orders_customer')['rows'], [{'total': 1}])
        result = self.query('SELECT name FROM orders_customer WHERE name = %s OR name = %s', ['Bob', 'Alice'])
        self.assertEqual(result['rows'], [{'name': 'Alice'}])
        self.assertEqual(self.query('SELECT phone AS public_name FROM orders_customer')['rows'], [{'public_name': None}])
        self.assertEqual(self.query('SELECT name FROM orders_customer WHERE tenant_id = %s', [self.b.pk])['rows'], [])
        self.assertEqual(self.query('SELECT name, COUNT(*) AS total FROM orders_customer GROUP BY name ORDER BY total DESC')['rows'], [{'name': 'Alice', 'total': 1}])

    def test_child_table_inherits_ownership_and_params_keep_position(self):
        for tenant in (self.a, self.b):
            item = MenuItem.objects.create(tenant=tenant, name='Same item')
            MenuItemVariant.objects.create(menu_item=item, size='small', price=10)
        self.assertEqual(self.query('SELECT COUNT(*) AS total FROM orders_menuitemvariant WHERE size = %s', ['small'])['rows'], [{'total': 1}])

    def test_untrusted_sql_shapes_are_rejected_even_when_model_calls_them_safe(self):
        malicious = [
            'SELECT name FROM orders_customer UNION SELECT username FROM auth_user',
            'SELECT name FROM orders_customer; SELECT username FROM auth_user;',
            'SELECT name FROM orders_customer WHERE name = %s OR 1=1',
            'SELECT (SELECT password FROM auth_user) FROM orders_customer',
            'SELECT pg_read_file(%s) FROM orders_customer',
            'SELECT name FROM orders_customer JOIN auth_user ON 1=1',
            'SELECT name FROM public.orders_customer',
            'SELECT name INTO stolen FROM orders_customer',
            'SELECT name FROM orders_customer -- tenant_id = 1',
            'SELECT name FROM orders_customer FOR UPDATE',
            'WITH deleted AS (DELETE FROM orders_customer RETURNING *) SELECT * FROM deleted',
            'DELETE FROM orders_customer',
        ]
        for sql in malicious:
            with self.subTest(sql=sql), self.assertRaises(ExecutionError):
                self.query(sql)
        with self.assertRaises(ExecutionError):
            self.query('SELECT name FROM orders_customer', enforce_tenant=False)
        self.assertEqual(CoreCustomer.objects.count(), 2)

    def test_legacy_order_creation_rejects_foreign_customer_item_and_variant(self):
        from chatbot_core.logic.cafe.db_utils import create_order
        item = MenuItem.objects.create(tenant=self.b, name='Foreign')
        variant = MenuItemVariant.objects.create(menu_item=item, size='small', price=10)
        line = {'item': item, 'variant': variant, 'item_name': 'Foreign', 'quantity': 1, 'unit_price': '10'}
        with self.assertRaises(ValueError):
            create_order(self.a, self.cb, [], 'chat')
        with self.assertRaises(ValueError):
            create_order(self.a, self.ca, [line], 'chat')
        self.assertFalse(CoreOrder.objects.exists())

    def test_session_and_address_mutations_reject_foreign_relationships(self):
        from chatbot_core.chat_session import create_new_chat_session, update_chat_session_order
        from chatbot_core.logic.cafe.db_utils import create_address
        from orders.models import ChatSession
        with self.assertRaises(ValueError):
            create_new_chat_session(self.a, self.cb, 'website', 'chat')
        session = create_new_chat_session(self.a, self.ca, 'website', 'chat', defaults={'tenant_id': self.b.pk, 'customer': self.cb})
        self.assertEqual(session.tenant_id, self.a.pk)
        self.assertEqual(session.customer_id, self.ca.pk)
        foreign = CoreOrder.objects.create(tenant=self.b, customer=self.cb, total_amount=0)
        with self.assertRaises(ValueError):
            update_chat_session_order(self.a, 'chat', foreign, 'website')
        session.refresh_from_db()
        self.assertIsNone(session.order_id)
        with self.assertRaises(ValueError):
            create_address(tenant=self.a, customer=self.cb, formatted_address='Foreign', components={})


