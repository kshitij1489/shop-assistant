"""Run with studio_desk.settings and an isolated PostgreSQL/Redis deployment."""
import io
import os
from unittest.mock import Mock, patch
from django.contrib.auth import get_user_model
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from chatbot_core.models import TenantInfo, TenantJSONDoc, TenantRuntimeConfiguration
from orders.models import MenuItem
from users.models import TenantProfile


@override_settings(PUBLIC_URL='http://testserver', ALLOWED_HOSTS=['testserver'],
                   SECURE_SSL_REDIRECT=False, SESSION_COOKIE_SECURE=False,
                   CSRF_COOKIE_SECURE=False, SIGNUP_ALERT_EMAIL='', LEGACY_TENANT_SYNC_ENABLED=False)
class InstallationTests(TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, DEMO_OWNER_PASSWORD='test-only-demo-password'))

    def seed(self):
        call_command('seed_cafe_demo', stdout=io.StringIO())
        return TenantInfo.objects.get(slug='demo-cafe')

    def test_seed_smoke_and_repeat_preserve_data(self):
        tenant = self.seed()
        user = get_user_model().objects.get(username='demo-owner')
        publication = TenantRuntimeConfiguration.objects.get(tenant=tenant)
        tenant.display_name = 'Owner edit'
        tenant.save()
        original_password, original_key = user.password, tenant.api_key
        self.seed()
        user.refresh_from_db()
        tenant.refresh_from_db()
        self.assertEqual(tenant.display_name, 'Owner edit')
        self.assertEqual(user.password, original_password)
        self.assertEqual(tenant.api_key, original_key)
        self.assertEqual(TenantRuntimeConfiguration.objects.get(tenant=tenant).version, publication.version)
        self.assertEqual(MenuItem.objects.filter(tenant=tenant).count(), 3)
        self.assertTrue(self.client.login(username='demo-owner', password='test-only-demo-password'))
        for path in ('/accounts/tenant-dashboard/', '/accounts/tenant-dashboard/menu/',
                     '/accounts/tenant-dashboard/knowledge/', '/accounts/tenant-dashboard/settings/'):
            self.assertEqual(self.client.get(path).status_code, 200, path)
        call_command('smoke_installation', stdout=io.StringIO())

    def test_seed_refuses_existing_account_or_non_demo_tenant(self):
        get_user_model().objects.create_user(username='demo-owner')
        with self.assertRaises(CommandError):
            self.seed()
        self.assertFalse(TenantInfo.objects.exists())
        TenantInfo.objects.create(slug='demo-cafe', display_name='Real business')
        with self.assertRaises(CommandError):
            self.seed()

    def test_signup_approval_and_publication_without_mongo_or_provider_keys(self):
        with patch('users.views.task_sync_tenant_from_folder.delay') as sync:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post('/accounts/signup/', {
                    'username': 'new-owner', 'email': 'owner@example.org',
                    'password': 'test-only-new-owner-password', 'business_name': 'New Café', 'business_type': 'cafe',
                    'password2': 'test-only-new-owner-password',
                })
            self.assertEqual(response.status_code, 302)
            sync.assert_not_called()
        tenant = TenantInfo.objects.get(display_name='New Café')
        self.assertEqual(tenant.approval_status, 'PENDING')
        self.assertEqual(self.client.get('/agent_core/token/?tenant=' + tenant.slug,
                                        HTTP_X_API_KEY=tenant.api_key).status_code, 404)
        admin = get_user_model().objects.create_superuser('administrator', password='test-only-admin-password')
        self.client.force_login(admin)
        self.assertEqual(self.client.get('/accounts/master-dashboard/').status_code, 200)
        self.assertEqual(self.client.post(f'/accounts/approve-tenant/{tenant.pk}/').status_code, 302)
        tenant.refresh_from_db()
        self.assertEqual(tenant.approval_status, 'APPROVED')
        self.client.force_login(TenantProfile.objects.get(tenant=tenant).user)
        publication = TenantRuntimeConfiguration.objects.get(tenant=tenant)
        TenantJSONDoc.objects.filter(tenant=tenant, dtype='response_intents', intent='general',
                                     sub_intent='greeting').update(payload='Welcome to the new café!')
        self.assertEqual(self.client.post('/accounts/tenant-dashboard/knowledge/',
                         {'action': 'publish', 'version': publication.version}).status_code, 302)
        publication.refresh_from_db()
        self.assertGreater(publication.version, 1)

    @override_settings(PUBLIC_URL='https://cafe.example.org')
    def test_telegram_uses_public_url_and_can_reregister(self):
        self.seed()
        self.client.force_login(get_user_model().objects.get(username='demo-owner'))
        with patch('users.views.requests.post', return_value=Mock(ok=True, json=lambda: {'ok': True})) as post:
            for _ in range(2):
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post('/accounts/tenant-dashboard/settings/', {'telegram_bot_token': '123:demo'})
                self.assertEqual(response.status_code, 302)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args.kwargs['data']['url'],
                             'https://cafe.example.org/agent_core/telegram-webhook/?token=123:demo')

    def test_local_telegram_registration_does_not_contact_provider(self):
        self.seed()
        self.client.force_login(get_user_model().objects.get(username='demo-owner'))
        with patch('users.views.requests.post') as post, self.captureOnCommitCallbacks(execute=True):
            self.client.post('/accounts/tenant-dashboard/settings/', {'telegram_bot_token': '123:demo'})
        post.assert_not_called()

    def test_health_reports_dependency_failure(self):
        with patch('studio_desk.health.cache.set', side_effect=ConnectionError('Redis unavailable')):
            with self.assertLogs('studio_desk.health', level='ERROR') as logs:
                response = self.client.get('/health')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {'status': 'unavailable'})
        self.assertIn('Readiness check failed', logs.output[0])
        self.assertIsNotNone(logs.records[0].exc_info)

    @override_settings(MONGO_DB_URL='')
    def test_legacy_sync_requires_explicit_mongo_url(self):
        with self.assertRaisesMessage(CommandError, 'Legacy MongoDB sync requires'):
            call_command('sync_tenants')
