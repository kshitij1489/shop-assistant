"""Regression coverage for dashboard access and state-changing endpoints."""
import json

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from chatbot_core.channels.website import public_jwt_token
from chatbot_core.models import TenantInfo, TenantJSONDoc
from users.models import TenantProfile


@override_settings(
    ROOT_URLCONF='tests.support.urls', JWT_SECRET='review-secret-with-at-least-32-characters',
    MIDDLEWARE=[
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.middleware.csrf.CsrfViewMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'django.contrib.messages.middleware.MessageMiddleware',
    ],
)
class DashboardAccessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = TenantInfo.objects.create(display_name='Review cafe', approval_status='APPROVED')
        self.user = get_user_model().objects.create_user('review-tenant')
        self.profile = TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.upload_url = reverse('tenant:upload_knowledge_prompt')
        self.token_url = reverse('tenant:generate_jwt_token')
        self.upload = {'dtype': 'knowledge', 'json_blob': json.dumps({
            'document_type': 'knowledge', 'documents': {
                'information_about_the_cafe': {'location_and_hours': 'Open daily'}},
        })}

    def assert_no_writes(self, client, expected_status):
        self.assertEqual(client.post(self.upload_url, self.upload, HTTP_ACCEPT='application/json').status_code, expected_status)
        self.assertEqual(client.post(self.token_url, HTTP_ACCEPT='application/json').status_code, expected_status)
        self.assertFalse(TenantJSONDoc.objects.exists())
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.last_jwt_token)
        self.assertIsNone(self.profile.last_token_generated_at)

    def test_anonymous_and_accounts_without_tenants_cannot_write(self):
        self.assert_no_writes(Client(), 302)
        for name, profile in [('no-profile', False), ('no-tenant', True)]:
            user = get_user_model().objects.create_user(name)
            if profile:
                TenantProfile.objects.create(user=user)
            self.client.force_login(user)
            self.assert_no_writes(self.client, 302)

    def test_unapproved_and_inactive_tenants_cannot_write(self):
        for active, status in [(True, 'PENDING'), (True, 'REJECTED'), (False, 'APPROVED')]:
            with self.subTest(active=active, status=status):
                self.tenant.is_active, self.tenant.approval_status = active, status
                self.tenant.save()
                self.assert_no_writes(self.client, 403)
        self.tenant.is_active, self.tenant.approval_status = True, 'PENDING'
        self.tenant.save()
        self.assertRedirects(self.client.get(self.upload_url), reverse('pending_review'), fetch_redirect_response=False)

    def test_master_impersonation_does_not_bypass_tenant_gate(self):
        self.profile.is_master = True
        self.profile.save()
        session = self.client.session
        session['impersonated_tenant_id'] = self.tenant.pk
        session.save()
        self.assert_no_writes(self.client, 302)

    def test_approved_tenant_can_upload_and_generate_or_reuse_token(self):
        self.assertEqual(self.client.post(self.upload_url, self.upload).status_code, 302)
        doc = TenantJSONDoc.objects.get()
        self.assertEqual(doc.tenant_id, self.tenant.pk)
        self.assertEqual((doc.intent, doc.sub_intent), ('information_about_the_cafe', 'location_and_hours'))
        self.assertEqual(doc.payload, 'Open daily')
        response = self.client.post(self.token_url)
        self.assertEqual(response.status_code, 200)
        token = response.json()['token']
        self.assertEqual(jwt.decode(token, settings.JWT_SECRET, algorithms=['HS256'])['tenant_slug'], self.tenant.slug)
        self.assertEqual(self.client.post(self.token_url).json()['token'], token)
        self.tenant.approval_status = 'REJECTED'
        self.tenant.save()
        self.assertEqual(self.client.post(self.token_url, HTTP_ACCEPT='application/json').status_code, 403)

    def test_token_generation_requires_post_and_csrf(self):
        self.assertEqual(self.client.get(self.token_url).status_code, 405)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.token_url).status_code, 403)
        client.get(reverse('tenant:tenant_settings'))
        self.assertEqual(client.post(self.token_url, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value).status_code, 200)

    def test_impersonation_requires_post_csrf_and_master_authority(self):
        start = reverse('impersonate_tenant', args=[self.tenant.pk])
        stop = reverse('stop_impersonation')
        self.assertEqual(self.client.post(start).status_code, 403)
        self.assertNotIn('impersonated_tenant_id', self.client.session)
        master = get_user_model().objects.create_superuser('review-master', 'master@example.com', 'password')
        client = Client(enforce_csrf_checks=True)
        client.force_login(master)
        for url in (start, stop):
            self.assertEqual(client.get(url).status_code, 405)
            self.assertEqual(client.post(url).status_code, 403)
        page = client.get(reverse('master_dashboard'))
        self.assertContains(page, f'method="post" action="{start}"')
        csrf = client.cookies['csrftoken'].value
        self.assertEqual(client.post(start, HTTP_X_CSRFTOKEN=csrf).status_code, 302)
        self.assertEqual(client.session['impersonated_tenant_id'], self.tenant.pk)
        self.assertEqual(client.get(stop).status_code, 405)
        self.assertEqual(client.post(stop).status_code, 403)
        self.assertEqual(client.session['impersonated_tenant_id'], self.tenant.pk)
        self.assertContains(client.get(reverse('master_dashboard')), f'method="post" action="{stop}"')
        self.assertEqual(client.post(stop, HTTP_X_CSRFTOKEN=csrf).status_code, 302)
        self.assertNotIn('impersonated_tenant_id', client.session)

    def test_public_api_key_mismatch_returns_403_for_any_length(self):
        factory = RequestFactory()
        for key in ('x', self.tenant.api_key + 'x', 'x' * 4096, 'é' * len(self.tenant.api_key)):
            with self.subTest(length=len(key)):
                request = factory.get('/token/', {'tenant': self.tenant.slug}, HTTP_X_API_KEY=key)
                self.assertEqual(public_jwt_token(request).status_code, 403)
        request = factory.get('/token/', {'tenant': self.tenant.slug}, HTTP_X_API_KEY=self.tenant.api_key)
        self.assertEqual(public_jwt_token(request).status_code, 200)
