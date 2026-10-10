"""Deleting unused setup must preserve business history and accurately clean up."""
from copy import deepcopy
from fnmatch import fnmatchcase
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from redis.exceptions import ConnectionError as RedisConnectionError

from chatbot_core import active_chats
from chatbot_core.models import TenantInfo, TenantRuntimeConfiguration
from commerce.models import AcceptedOrder, Configuration, Connection, Location, StockItem
from orders.models import AddonGroup, ChatSession, CheckoutSettings, Customer, MenuCategory, MenuItem, Order, Tax
from orders.onboarding import initialize_ordering_settings
from users.models import TenantProfile


class ChatRedis:
    """Redis transport fake; transcript serialization and key matching are real."""
    def __init__(self):
        self.data = {}
        self.transactions = []
        self.fail_execute = False

    def set(self, key, value):
        self.data[key] = value

    def hset(self, key, mapping):
        self.data.setdefault(key, {}).update(mapping)

    def zadd(self, key, mapping):
        self.data.setdefault(key, {}).update(mapping)

    def rpush(self, key, value):
        self.data.setdefault(key, []).append(value)

    def ltrim(self, key, start, end):
        self.data[key] = self.data[key][start:end + 1 if end != -1 else None]

    def scan_iter(self, *, match, count):
        for key in list(self.data):
            if fnmatchcase(key, match):
                yield key.encode()

    def pipeline(self, *, transaction):
        self.transactions.append(transaction)
        store = self

        class Pipeline:
            def __init__(self):
                self.batches = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def delete(self, *keys):
                self.batches.append(keys)

            def execute(self):
                if store.fail_execute:
                    raise RedisConnectionError('offline')
                counts = []
                for batch in self.batches:
                    count = 0
                    for raw_key in batch:
                        key = raw_key.decode()
                        count += int(key in store.data)
                        store.data.pop(key, None)
                    counts.append(count)
                return counts

        return Pipeline()


class TenantDeletionTests(TestCase):
    def setUp(self):
        self.master = get_user_model().objects.create_user('delete-master')
        TenantProfile.objects.create(user=self.master, is_master=True)
        self.tenant = TenantInfo.objects.create(display_name='Unused business', approval_status='APPROVED')
        self.tenant_id = self.tenant.pk
        self.owner = get_user_model().objects.create_user('delete-owner', email='owner@example.test', password='test-owner-password')
        TenantProfile.objects.create(user=self.owner, tenant=self.tenant)
        self.client.force_login(self.master)
        self.url = reverse('delete_tenant', args=[self.tenant_id])
        self.redis = ChatRedis()
        self.enterContext(patch.object(active_chats, '_r', self.redis))

    def transcript(self, tenant_id=None, *, chat='guest', channel='website'):
        tenant_id = self.tenant_id if tenant_id is None else tenant_id
        active_chats.append_message(str(tenant_id), channel, chat, direction='in', text='My phone is +919876543210')
        active_chats.touch_active_chat(str(tenant_id), channel, chat, phone='+919876543210')
        active_chats.set_global_agent_enabled(str(tenant_id), channel, False)

    def assert_not_deleted(self, response):
        self.assertEqual(response.status_code, 302)
        notices = [str(message) for message in get_messages(response.wsgi_request)]
        self.assertIn('Tenant Not Deleted', notices)
        self.assertNotIn('Tenant Deleted', notices)
        self.assertTrue(TenantInfo.objects.filter(pk=self.tenant_id).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.owner.pk).exists())
        self.assertTrue(TenantProfile.objects.filter(user=self.owner, tenant_id=self.tenant_id).exists())

    def test_order_without_accepted_order_preserves_tenant_customer_history_and_setup(self):
        checkout, config = initialize_ordering_settings(self.tenant)
        customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='+919876543210')
        order = Order.objects.create(tenant=self.tenant, customer=customer, source='inhouse', total_amount='100')
        chat = ChatSession.objects.create(tenant=self.tenant, customer=customer, session_id='guest', platform='website', order=order)
        self.transcript()
        redis_before = deepcopy(self.redis.data)
        self.assertFalse(AcceptedOrder.objects.exists())
        self.assertFalse(Connection.objects.exists())
        self.assertFalse(StockItem.objects.exists())
        with self.assertLogs('users.views', level='WARNING') as logs:
            response = self.client.post(self.url)
        self.assert_not_deleted(response)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertTrue(Customer.objects.filter(pk=customer.pk, phone='+919876543210').exists())
        self.assertTrue(ChatSession.objects.filter(pk=chat.pk).exists())
        self.assertTrue(Configuration.objects.filter(pk=config.pk).exists())
        self.assertTrue(Location.objects.filter(pk=config.location_id).exists())
        self.assertTrue(type(checkout).objects.filter(pk=checkout.pk).exists())
        self.assertEqual(self.redis.data, redis_before)
        self.assertIn(f'user_id={self.master.pk} tenant_id={self.tenant_id}', '\n'.join(logs.output))

    def test_order_without_customer_or_commerce_configuration_also_blocks_cascade(self):
        order = Order.objects.create(tenant=self.tenant, customer=None, source='inhouse', total_amount='100')
        response = self.client.post(self.url)
        self.assert_not_deleted(response)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_customer_without_orders_blocks_deletion(self):
        customer = Customer.objects.create(tenant=self.tenant, name='Guest', phone='+919876543210')
        response = self.client.post(self.url)
        self.assert_not_deleted(response)
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())

    def test_menu_rows_without_orders_block_deletion(self):
        for model, fields in ((MenuItem, {'name': 'Coffee'}), (MenuCategory, {'name': 'Drinks'}),
                              (AddonGroup, {'name': 'Extras'}), (Tax, {'title': 'GST', 'rate_display': '5%'})):
            with self.subTest(model=model.__name__):
                row = model.objects.create(tenant=self.tenant, **fields)
                self.assert_not_deleted(self.client.post(self.url))
                self.assertTrue(model.objects.filter(pk=row.pk).exists())
                row.delete()

    def test_protected_connection_restores_onboarding_and_logs_blocked_outcome(self):
        checkout, config = initialize_ordering_settings(self.tenant)
        connection = Connection.objects.create(location=config.location, provider='test', role='pos',
            account_id='unused-account', secret_ref='test-key', capabilities=[])
        self.transcript()
        redis_before = deepcopy(self.redis.data)
        with self.assertLogs('users.views', level='WARNING') as logs:
            response = self.client.post(self.url)
        self.assert_not_deleted(response)
        self.assertTrue(Configuration.objects.filter(pk=config.pk).exists())
        self.assertTrue(Location.objects.filter(pk=config.location_id).exists())
        self.assertTrue(Connection.objects.filter(pk=connection.pk).exists())
        self.assertTrue(type(checkout).objects.filter(pk=checkout.pk).exists())
        self.assertEqual(self.redis.data, redis_before)
        self.assertIn(f'user_id={self.master.pk} tenant_id={self.tenant_id} reason=protected_records', '\n'.join(logs.output))

    def test_accepted_order_history_is_never_removed(self):
        _, config = initialize_ordering_settings(self.tenant)
        order = Order.objects.create(tenant=self.tenant, customer=None, source='inhouse', total_amount='100')
        accepted = AcceptedOrder.objects.create(order=order, location=config.location, currency='INR',
            total_minor=10000, snapshot={}, snapshot_hash='a' * 64, expires_at=timezone.now())
        self.assert_not_deleted(self.client.post(self.url))
        self.assertTrue(AcceptedOrder.objects.filter(pk=accepted.pk).exists())
        self.assertTrue(Configuration.objects.filter(pk=config.pk).exists())

    def test_stock_and_menu_survive_a_blocked_deletion(self):
        _, config = initialize_ordering_settings(self.tenant)
        item = MenuItem.objects.create(tenant=self.tenant, name='Coffee')
        stock = StockItem.objects.create(location=config.location, item=item, mode='quantity', on_hand=10)
        self.assert_not_deleted(self.client.post(self.url))
        self.assertTrue(StockItem.objects.filter(pk=stock.pk).exists())
        self.assertTrue(MenuItem.objects.filter(pk=item.pk).exists())
        self.assertTrue(Configuration.objects.filter(pk=config.pk).exists())

    def test_unused_onboarded_tenant_removes_setup_owner_accounts_and_all_chat_namespaces(self):
        checkout, config = initialize_ordering_settings(self.tenant)
        TenantRuntimeConfiguration.objects.create(tenant=self.tenant)
        second_owner = get_user_model().objects.create_user('second-delete-owner')
        TenantProfile.objects.create(user=second_owner, tenant=self.tenant)
        owner_client = Client()
        owner_client.force_login(self.owner)
        other = TenantInfo.objects.create(pk=self.tenant_id * 10, display_name='Other business')
        self.transcript()
        self.transcript(channel='telegram', chat='123')
        self.transcript(other.pk)
        other_data = {key: deepcopy(value) for key, value in self.redis.data.items() if f':{other.pk}:' in key}
        self.redis.data[f'msgs:{self.tenant_id}:whatsapp:orphan'] = ['Personal data without an index']
        self.redis.data['unrelated:cache'] = 'keep'
        with self.assertLogs('users.views', level='INFO') as logs:
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('Tenant Deleted', [str(message) for message in get_messages(response.wsgi_request)])
        self.assertFalse(TenantInfo.objects.filter(pk=self.tenant_id).exists())
        self.assertFalse(Configuration.objects.filter(pk=config.pk).exists())
        self.assertFalse(Location.objects.filter(pk=config.location_id).exists())
        self.assertFalse(type(checkout).objects.filter(pk=checkout.pk).exists())
        self.assertFalse(get_user_model().objects.filter(pk__in=[self.owner.pk, second_owner.pk]).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.master.pk).exists())
        self.assertTrue(TenantInfo.objects.filter(pk=other.pk).exists())
        self.assertEqual(self.redis.data, {**other_data, 'unrelated:cache': 'keep'})
        self.assertEqual(self.redis.transactions, [True])
        self.assertFalse(owner_client.login(username='delete-owner', password='test-owner-password'))
        self.assertEqual(owner_client.get(reverse('tenant:tenant_dashboard')).status_code, 302)
        self.assertIn(f'user_id={self.master.pk} tenant_id={self.tenant_id} owner_accounts=2', '\n'.join(logs.output))

    def test_chat_scan_failure_rolls_back_sql_and_does_not_report_success(self):
        _, config = initialize_ordering_settings(self.tenant)
        self.transcript()
        before = deepcopy(self.redis.data)
        scan = self.redis.scan_iter

        def fail_after_transcript_scan(**kwargs):
            if kwargs['match'].startswith('ac:'):
                raise RedisConnectionError('offline')
            return scan(**kwargs)

        with patch.object(self.redis, 'scan_iter', side_effect=fail_after_transcript_scan), \
                self.assertLogs('users.views', level='WARNING') as logs:
            response = self.client.post(self.url)
        self.assert_not_deleted(response)
        self.assertTrue(Configuration.objects.filter(pk=config.pk).exists())
        self.assertTrue(Location.objects.filter(pk=config.location_id).exists())
        self.assertEqual(self.redis.data, before)
        self.assertIn('reason=chat_storage_unavailable', '\n'.join(logs.output))

    def test_redis_execution_failure_rolls_back_owner_and_tenant_deletion(self):
        initialize_ordering_settings(self.tenant)
        self.transcript()
        before = deepcopy(self.redis.data)
        self.redis.fail_execute = True
        response = self.client.post(self.url)
        self.assert_not_deleted(response)
        self.assertTrue(Configuration.objects.filter(tenant_id=self.tenant_id).exists())
        self.assertEqual(self.redis.data, before)

    def test_cleanup_batches_large_transcript_sets_and_keeps_other_tenants(self):
        for index in range(1100):
            self.redis.data[f'msgs:{self.tenant_id}:telegram:{index}'] = ['text']
        other_key = f'msgs:{self.tenant_id}0:telegram:1'
        self.redis.data[other_key] = ['keep']
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(TenantInfo.objects.filter(pk=self.tenant_id).exists())
        self.assertEqual(self.redis.data, {other_key: ['keep']})

    def test_operations_accounts_cannot_be_erased_with_a_tenant(self):
        for field in ('is_staff', 'is_superuser'):
            setattr(self.owner, field, True)
            self.owner.save(update_fields=[field])
            self.assert_not_deleted(self.client.post(self.url))
            setattr(self.owner, field, False)
            self.owner.save(update_fields=[field])
        TenantProfile.objects.filter(user=self.owner).update(is_master=True)
        self.assert_not_deleted(self.client.post(self.url))

    def test_missing_tenant_is_reported_and_logged(self):
        with self.assertLogs('users.views', level='WARNING') as logs:
            response = self.client.post(reverse('delete_tenant', args=[999999]))
        self.assertEqual(response.status_code, 302)
        self.assertIn('Tenant Not Found', [str(message) for message in get_messages(response.wsgi_request)])
        self.assertIn(f'user_id={self.master.pk} tenant_id=999999 reason=not_found', '\n'.join(logs.output))

    def test_deletion_requires_post_master_access_and_csrf(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.post(self.url).status_code, 302)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.master)
        self.assertEqual(csrf_client.post(self.url).status_code, 403)
        self.assertTrue(TenantInfo.objects.filter(pk=self.tenant_id).exists())


@override_settings(SIGNUP_ALERT_EMAIL='', LEGACY_TENANT_SYNC_ENABLED=False)
class RegisteredTenantDeletionTests(TestCase):
    def setUp(self):
        self.enterContext(patch.object(active_chats, '_r', ChatRedis()))
        response = self.client.post(reverse('signup'), {
            'username': 'cafe-owner', 'email': 'owner@example.org',
            'password': 'test-only-owner-password',
            'password2': 'test-only-owner-password',
            'business_name': 'Deletion Café', 'business_type': 'cafe',
        })
        self.assertEqual(response.status_code, 302)
        self.tenant = TenantInfo.objects.get(display_name='Deletion Café')
        self.config = Configuration.objects.get(tenant=self.tenant)
        self.location = self.config.location
        self.url = reverse('delete_tenant', args=[self.tenant.pk])
        self.master = get_user_model().objects.create_user(username='master')
        TenantProfile.objects.create(user=self.master, is_master=True)
        self.client.force_login(self.master)

    def assert_onboarding_preserved(self):
        self.assertTrue(TenantInfo.objects.filter(pk=self.tenant.pk).exists())
        self.assertTrue(Configuration.objects.filter(pk=self.config.pk).exists())
        self.assertTrue(Location.objects.filter(pk=self.location.pk).exists())
        self.assertTrue(CheckoutSettings.objects.filter(tenant=self.tenant).exists())
        self.assertTrue(TenantRuntimeConfiguration.objects.filter(tenant=self.tenant).exists())
        self.assertTrue(TenantProfile.objects.filter(tenant=self.tenant).exists())

    def test_master_can_delete_newly_registered_cafe(self):
        other_tenant = TenantInfo.objects.create(display_name='Other Café')
        from orders.onboarding import initialize_ordering_settings
        _, other_config = initialize_ordering_settings(other_tenant)
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, 'Tenant Deleted')
        self.assertFalse(TenantInfo.objects.filter(pk=self.tenant.pk).exists())
        self.assertFalse(Configuration.objects.filter(pk=self.config.pk).exists())
        self.assertFalse(Location.objects.filter(tenant_id=self.tenant.pk).exists())
        self.assertFalse(CheckoutSettings.objects.filter(tenant_id=self.tenant.pk).exists())
        self.assertFalse(TenantRuntimeConfiguration.objects.filter(tenant_id=self.tenant.pk).exists())
        self.assertFalse(TenantProfile.objects.filter(tenant_id=self.tenant.pk).exists())
        self.assertTrue(Configuration.objects.filter(pk=other_config.pk).exists())
        self.assertTrue(Location.objects.filter(pk=other_config.location_id).exists())

    def test_superuser_can_delete_newly_registered_cafe(self):
        admin = get_user_model().objects.create_superuser(username='admin', password='test-only')
        self.client.force_login(admin)
        self.assertRedirects(self.client.post(self.url), reverse('master_dashboard'))
        self.assertFalse(TenantInfo.objects.filter(pk=self.tenant.pk).exists())

    def test_commerce_history_blocks_deletion_and_restores_configuration(self):
        order = Order.objects.create(tenant=self.tenant, source=Order.Source.INHOUSE, total_amount='10.00')
        accepted = AcceptedOrder.objects.create(
            order=order, location=self.location, currency='INR', total_minor=1000,
            snapshot={'items': []}, snapshot_hash='test-snapshot', expires_at=timezone.now(),
        )
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, 'Tenant Not Deleted')
        self.assertContains(response, 'business records')
        self.assertNotContains(response, '>Tenant Deleted<')
        self.assert_onboarding_preserved()
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertTrue(AcceptedOrder.objects.filter(pk=accepted.pk).exists())

    def test_other_protected_commerce_records_also_block_deletion(self):
        connection = Connection.objects.create(
            location=self.location, provider='custom', role='pos', account_id='test-account',
        )
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, 'Tenant Not Deleted')
        self.assert_onboarding_preserved()
        self.assertTrue(Connection.objects.filter(pk=connection.pk).exists())

    def test_owner_cannot_delete_tenant(self):
        self.client.force_login(get_user_model().objects.get(username='cafe-owner'))
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.assert_onboarding_preserved()

    def test_get_does_not_delete_tenant(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assert_onboarding_preserved()

    def test_missing_tenant_has_controlled_response(self):
        response = self.client.post(reverse('delete_tenant', args=[self.tenant.pk + 1000]))
        self.assertRedirects(response, reverse('master_dashboard'))
        self.assertIn('Tenant Not Found', [str(message) for message in get_messages(response.wsgi_request)])
