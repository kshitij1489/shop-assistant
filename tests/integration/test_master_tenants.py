"""Master account creation and activation through the real dashboard views."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from chatbot_core.models import TenantInfo, TenantRuntimeConfiguration
from users.models import TenantProfile


@override_settings(SIGNUP_ALERT_EMAIL='', LEGACY_TENANT_SYNC_ENABLED=False,
                   LOGIN_REDIRECT_URL='dashboard')
class MasterTenantTests(TestCase):
    def setUp(self):
        self.master = get_user_model().objects.create_user(username='master')
        TenantProfile.objects.create(user=self.master, is_master=True)
        self.tenant = TenantInfo.objects.create(display_name='Existing Café', approval_status='APPROVED')
        self.owner = get_user_model().objects.create_user(username='existing-owner')
        TenantProfile.objects.create(user=self.owner, tenant=self.tenant)
        self.client.force_login(self.master)
        self.data = {
            'username': 'new-owner', 'email': 'owner@example.org',
            'password': 'new-owner-test-password', 'password2': 'new-owner-test-password',
            'business_name': 'New Café', 'business_type': 'cafe',
        }
        self.create_url = reverse('create_tenant')
        self.tenants_url = reverse('master_tenants')
        self.active_url = reverse('set_tenant_active', args=[self.tenant.pk])
        self.details_url = reverse('update_tenant_details', args=[self.tenant.pk])

    def _details_data(self, **overrides):
        profile = TenantProfile.objects.get(user=self.owner, tenant=self.tenant)
        prefix = f'tenant-{self.tenant.pk}'
        data = {
            f'{prefix}-display_name': self.tenant.display_name,
            f'{prefix}-address': self.tenant.address,
            f'{prefix}-username_{profile.pk}': self.owner.username,
        }
        data.update(overrides)
        return data

    def test_dashboard_links_to_tenants_and_no_longer_has_creation_form(self):
        response = self.client.get(reverse('master_dashboard'))
        self.assertContains(response, 'Pending Approvals')
        self.assertContains(response, '<h2 id="impersonate-title">Impersonate</h2>')
        self.assertContains(response, f'href="{self.tenants_url}"')
        self.assertNotContains(response, f'action="{self.create_url}"')

    def test_tenants_page_lists_accounts_and_activation_switches(self):
        response = self.client.get(self.tenants_url)
        self.assertContains(response, 'Existing Café')
        self.assertContains(response, 'existing-owner')
        self.assertContains(response, '<table class="all-tenants-table">')
        self.assertContains(response, 'role="switch" aria-checked="true"')
        self.assertContains(response, f'action="{self.active_url}"')
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="whatsapp_number"')
        self.assertContains(response, 'Telegram Bot Token:')
        self.assertContains(response, 'name="telegram_bot_token"')
        self.assertNotContains(response, 'name="telegram_chat_id"')
        self.assertContains(response, 'name="address"')
        self.assertContains(response, '<th scope="col">Address</th>')
        self.assertContains(response, '<span class="muted">—</span>')
        self.assertContains(response, '<summary>Edit Tenant</summary>')
        self.assertContains(response, 'id="edit-tenant-select"')
        self.assertContains(response, f'<option value="{self.tenant.pk}">Existing Café</option>')
        self.assertLess(response.content.decode().index('Create Tenant'), response.content.decode().index('Edit Tenant'))
        self.assertNotContains(response, f'action="{self.details_url}"')
        self.assertNotContains(response, f'name="tenant-{self.tenant.pk}-display_name"')
        self.assertNotContains(response, 'class="card tenant-edit" open')
        editing = self.client.get(f'{self.tenants_url}?edit={self.tenant.pk}')
        self.assertContains(editing, 'id="edit-tenant" open')
        self.assertContains(editing, f'action="{self.details_url}"')
        self.assertContains(editing, f'name="tenant-{self.tenant.pk}-display_name"')
        self.assertContains(editing, f'name="tenant-{self.tenant.pk}-username_{self.owner.tenantprofile.pk}"')
        self.assertContains(editing, f'value="{self.owner.username}"')
        self.assertContains(editing, f'Public link stays <strong>{self.tenant.slug}</strong>')
        self.assertContains(editing, 'data-save-edits disabled')
        self.assertContains(editing, 'data-tenant-edit-dialog')
        self.assertContains(editing, f'<option value="{self.tenant.pk}" selected>Existing Café</option>')
        missing = self.client.get(f'{self.tenants_url}?edit=99999')
        self.assertContains(missing, 'All Tenants')
        self.assertNotContains(missing, 'data-tenant-edit-form')
        self.assertNotContains(self.client.get(f'{self.tenants_url}?edit=nope'), 'data-tenant-edit-form')
        self.assertNotContains(response, '<select name="user"')
        self.assertNotContains(response, 'class="card tenant-create" open')

    def test_pending_approvals_are_first_and_actions_return_to_tenants(self):
        pending = TenantInfo.objects.create(display_name='Pending Café')
        response = self.client.get(self.tenants_url)
        html = response.content.decode()
        self.assertLess(html.index('Pending Approvals'), html.index('Create Tenant'))
        self.assertContains(response, f'action="{reverse("approve_tenant", args=[pending.pk])}"')
        self.assertContains(response, f'action="{reverse("reject_tenant", args=[pending.pk])}"')
        self.assertNotContains(response, f'action="{reverse("approve_tenant", args=[self.tenant.pk])}"')
        for action, status in [('approve_tenant', 'APPROVED'), ('reject_tenant', 'REJECTED')]:
            with self.subTest(action=action):
                pending.approval_status = 'PENDING'
                pending.save()
                response = self.client.post(reverse(action, args=[pending.pk]), {
                    'return_to': 'master_tenants', 'note': 'Reviewed here',
                })
                self.assertRedirects(response, self.tenants_url)
                pending.refresh_from_db()
                self.assertEqual(pending.approval_status, status)
                self.assertEqual(pending.review_note, 'Reviewed here')
                self.assertEqual(pending.reviewed_by_id, self.master.pk)
                self.assertContains(self.client.get(self.tenants_url), 'No pending tenants')

    def test_invalid_edit_selectors_do_not_open_an_editor(self):
        for value in ('', 'nope', '²', '9' * 5000):
            with self.subTest(value=value[:20]):
                response = self.client.get(self.tenants_url, {'edit': value})
                self.assertContains(response, 'All Tenants')
                self.assertNotContains(response, 'data-tenant-edit-form')

    def test_approval_redirect_defaults_to_dashboard_and_rejects_external_targets(self):
        for action in ('approve_tenant', 'reject_tenant'):
            for data in ({}, {'return_to': 'https://example.org/'}):
                with self.subTest(action=action, data=data):
                    self.assertRedirects(self.client.post(reverse(action, args=[self.tenant.pk]), data), reverse('master_dashboard'))

    def test_create_account_preserves_master_session_and_existing_assignments(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.create_url, {**self.data, 'user': self.owner.pk})
        self.assertRedirects(response, self.tenants_url)
        owner = get_user_model().objects.get(username='new-owner')
        self.assertTrue(owner.check_password(self.data['password']))
        self.assertEqual(owner.email, self.data['email'])
        self.assertFalse(owner.is_staff)
        self.assertFalse(owner.is_superuser)
        self.assertFalse(owner.tenantprofile.is_master)
        tenant = owner.tenantprofile.tenant
        self.assertEqual(tenant.display_name, self.data['business_name'])
        self.assertEqual(tenant.address, '')
        self.assertEqual(tenant.approval_status, 'PENDING')
        self.assertTrue(tenant.is_active)
        self.assertTrue(TenantRuntimeConfiguration.objects.filter(tenant=tenant).exists())
        self.assertEqual(int(self.client.session['_auth_user_id']), self.master.pk)
        self.assertEqual(TenantProfile.objects.get(user=self.owner).tenant_id, self.tenant.pk)
        self.assertTrue(TenantProfile.objects.get(user=self.master).is_master)
        self.assertContains(self.client.get(reverse('master_dashboard')), 'New Café')

        owner_client = Client()
        response = owner_client.post(reverse('login'), {
            'username': 'new-owner', 'password': self.data['password'],
        }, follow=True)
        self.assertEqual(int(owner_client.session['_auth_user_id']), owner.pk)
        self.assertEqual(response.redirect_chain[-1], (reverse('pending_review'), 302))

    def test_invalid_account_details_do_not_create_or_reassign_anything(self):
        for field, value in (
            ('username', self.owner.username), ('business_name', self.tenant.display_name),
            ('business_name', ''), ('email', 'invalid'), ('password2', 'mismatch'),
        ):
            with self.subTest(field=field, value=value):
                response = self.client.post(self.create_url, {**self.data, field: value})
                self.assertContains(response, 'Tenant was not created.')
                self.assertContains(response, 'class="card tenant-create" open')
                self.assertNotContains(response, self.data['password'])
                self.assertEqual(get_user_model().objects.count(), 2)
                self.assertEqual(TenantInfo.objects.count(), 1)
                self.assertEqual(TenantProfile.objects.get(user=self.owner).tenant_id, self.tenant.pk)

    def test_configuration_failure_rolls_back_account_tenant_and_profile(self):
        with patch('chatbot_core.runtime_configuration.publish_default_configuration', side_effect=RuntimeError('failed')):
            with self.assertRaises(RuntimeError):
                self.client.post(self.create_url, self.data)
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertEqual(TenantInfo.objects.count(), 1)
        self.assertEqual(TenantProfile.objects.count(), 2)

    @override_settings(SIGNUP_ALERT_EMAIL='master@example.org',
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_creation_sends_master_creation_alert_without_password(self):
        from django.core import mail
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.create_url, self.data)
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith('Master-created tenant:'))
        self.assertIn(f'user ID: {self.master.pk}', mail.outbox[0].body)
        self.assertIn('New Café', mail.outbox[0].body)
        self.assertIn('Address: not provided', mail.outbox[0].body)
        self.assertNotIn(self.data['password'], mail.outbox[0].body)

    def test_master_can_attach_channels_before_approval(self):
        response = self.client.post(self.create_url, {
            **self.data, 'whatsapp_number': ' +15551234567 ', 'telegram_bot_token': ' 123456:test-bot-token ',
        })
        self.assertRedirects(response, self.tenants_url)
        tenant = TenantInfo.objects.get(display_name=self.data['business_name'])
        self.assertEqual(tenant.whatsapp_number, '+15551234567')
        self.assertEqual(tenant.telegram_bot_token, '123456:test-bot-token')
        self.assertFalse(tenant.telegram_chat_id)
        self.assertEqual(tenant.approval_status, 'PENDING')

    def test_duplicate_bot_token_shows_error_without_creating_an_account(self):
        token = '123456:existing-test-bot'
        self.tenant.telegram_bot_token = token
        self.tenant.save(update_fields=['telegram_bot_token'])
        response = self.client.post(self.create_url, {**self.data, 'telegram_bot_token': token})
        self.assertContains(response, 'This Telegram bot token is already in use.')
        self.assertNotContains(response, token)
        self.assertEqual(get_user_model().objects.count(), 2)
        self.assertEqual(TenantInfo.objects.count(), 1)

    def test_optional_address_is_stored_and_wraps_in_the_tenant_list(self):
        long_address = '12 Example Road, ' * 12
        self.tenant.address = long_address.strip()
        self.tenant.save(update_fields=['address'])
        response = self.client.get(self.tenants_url)
        self.assertContains(response, 'class="tenant-address-text"')
        self.assertContains(response, long_address.strip())
        address = '14 Park Street\nKolkata 700016'
        response = self.client.post(self.create_url, {**self.data, 'address': f'  {address}  '})
        self.assertRedirects(response, self.tenants_url)
        created = TenantInfo.objects.get(display_name=self.data['business_name'])
        self.assertEqual(created.address, address)
        listed = self.client.get(self.tenants_url)
        self.assertContains(listed, '14 Park Street')
        self.assertContains(listed, 'class="tenant-address-text"')

    def test_activation_logs_actor_tenant_and_new_state(self):
        for value, expected in [('false', False), ('true', True)]:
            with self.subTest(value=value), self.assertLogs('users.views', level='INFO') as logs:
                self.client.post(self.active_url, {'is_active': value})
            self.assertIn(
                f'user_id={self.master.pk} tenant_id={self.tenant.pk} is_active={expected}',
                '\n'.join(logs.output),
            )

    def test_activation_controls_existing_session_access_and_is_idempotent(self):
        owner_client = Client()
        owner_client.force_login(self.owner)
        dashboard = reverse('tenant:tenant_dashboard')
        self.assertEqual(owner_client.get(dashboard).status_code, 200)
        for _ in range(2):
            self.assertRedirects(self.client.post(self.active_url, {'is_active': 'false'}), self.tenants_url)
            self.tenant.refresh_from_db()
            self.assertFalse(self.tenant.is_active)
            self.assertEqual(self.tenant.approval_status, 'APPROVED')
            response = owner_client.get(dashboard, follow=True)
            self.assertEqual(response.redirect_chain, [(reverse('pending_review'), 302)])
            self.assertContains(response, 'Tenant inactive')
            self.assertNotContains(response, 'will enable your dashboard shortly')
            for headers in ({'HTTP_ACCEPT': 'application/json'}, {'HTTP_X_REQUESTED_WITH': 'XMLHttpRequest'}):
                response = owner_client.get(dashboard, **headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {'detail': 'This tenant is inactive.'})
        self.assertContains(self.client.get(self.tenants_url), 'role="switch" aria-checked="false"')
        self.assertRedirects(self.client.post(self.active_url, {'is_active': 'true'}), self.tenants_url)
        self.assertEqual(owner_client.get(dashboard).status_code, 200)

    def test_activation_does_not_approve_pending_or_rejected_tenants(self):
        for status in ('PENDING', 'REJECTED'):
            with self.subTest(status=status):
                self.tenant.approval_status = status
                self.tenant.is_active = False
                self.tenant.save()
                self.client.post(self.active_url, {'is_active': 'true'})
                self.tenant.refresh_from_db()
                self.assertTrue(self.tenant.is_active)
                self.assertEqual(self.tenant.approval_status, status)
                owner_client = Client()
                owner_client.force_login(self.owner)
                self.assertRedirects(owner_client.get(reverse('tenant:tenant_dashboard')), reverse('pending_review'))

    def test_mutations_require_post_valid_state_and_existing_tenant(self):
        self.assertEqual(self.client.get(self.create_url).status_code, 405)
        self.assertEqual(self.client.get(self.active_url).status_code, 405)
        self.assertEqual(self.client.get(self.details_url).status_code, 405)
        for value in ('', 'yes'):
            self.assertEqual(self.client.post(self.active_url, {'is_active': value}).status_code, 400)
        self.assertEqual(self.client.post(self.active_url, {}).status_code, 400)
        self.tenant.refresh_from_db()
        self.assertTrue(self.tenant.is_active)
        self.assertEqual(self.client.post(reverse('set_tenant_active', args=[99999]), {'is_active': 'false'}).status_code, 404)
        self.assertEqual(self.client.post(reverse('update_tenant_details', args=[99999]), {}).status_code, 404)

    def test_regular_users_and_anonymous_visitors_cannot_manage_tenants(self):
        for user in (self.owner, None):
            with self.subTest(user=user):
                client = Client()
                if user:
                    client.force_login(user)
                self.assertEqual(client.get(self.tenants_url).status_code, 302)
                self.assertEqual(client.post(self.create_url, self.data).status_code, 302)
                self.assertEqual(client.post(self.active_url, {'is_active': 'false'}).status_code, 302)
                self.assertEqual(client.post(self.details_url, self._details_data(**{
                    f'tenant-{self.tenant.pk}-display_name': 'Stolen name',
                })).status_code, 302)
                self.tenant.refresh_from_db()
                self.owner.refresh_from_db()
                self.assertEqual(self.tenant.display_name, 'Existing Café')
                self.assertEqual(self.owner.username, 'existing-owner')
                self.assertTrue(self.tenant.is_active)
                self.assertEqual(TenantInfo.objects.count(), 1)
                self.assertFalse(get_user_model().objects.filter(username='new-owner').exists())
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(reverse('tenant:tenant_dashboard')), f'href="{self.tenants_url}"')

    def test_superuser_without_profile_can_manage_tenants(self):
        admin = get_user_model().objects.create_superuser('admin', 'admin@example.org', 'test-password')
        self.client.force_login(admin)
        self.assertContains(self.client.get(self.tenants_url), f'href="{self.tenants_url}"')
        self.assertRedirects(self.client.post(self.create_url, self.data), self.tenants_url)
        self.assertRedirects(self.client.post(self.active_url, {'is_active': 'false'}), self.tenants_url)
        self.assertRedirects(self.client.post(self.details_url, self._details_data(**{
            f'tenant-{self.tenant.pk}-display_name': 'Admin Renamed',
            f'tenant-{self.tenant.pk}-address': 'Admin street',
        })), self.tenants_url)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.display_name, 'Admin Renamed')
        self.assertEqual(self.tenant.address, 'Admin street')
        self.assertEqual(int(self.client.session['_auth_user_id']), admin.pk)

    def test_master_can_edit_name_username_and_address_without_changing_the_public_link(self):
        self.owner.set_password('keep-this-password')
        self.owner.email = 'owner@example.org'
        self.owner.save()
        original_slug = self.tenant.slug
        address = '14 Park Street\nKolkata 700016'
        prefix = f'tenant-{self.tenant.pk}'
        data = self._details_data(**{
            f'{prefix}-display_name': '  Morning Roasters  ',
            f'{prefix}-address': f'  {address}  ',
            f'{prefix}-username_{self.owner.tenantprofile.pk}': 'renamed-owner',
        })
        data['slug'] = 'hijacked'
        with self.assertLogs('users.views', level='INFO') as logs:
            response = self.client.post(self.details_url, data)
        self.assertRedirects(response, self.tenants_url)
        self.assertIn(
            f'user_id={self.master.pk} tenant_id={self.tenant.pk} changed=display_name,address,username',
            '\n'.join(logs.output),
        )
        self.tenant.refresh_from_db()
        self.owner.refresh_from_db()
        self.assertEqual(self.tenant.display_name, 'Morning Roasters')
        self.assertEqual(self.tenant.address, address)
        self.assertEqual(self.tenant.slug, original_slug)
        self.assertEqual(self.tenant.approval_status, 'APPROVED')
        self.assertTrue(self.tenant.is_active)
        self.assertEqual(self.owner.username, 'renamed-owner')
        self.assertEqual(self.owner.email, 'owner@example.org')
        self.assertTrue(self.owner.check_password('keep-this-password'))
        listed = self.client.get(self.tenants_url)
        self.assertContains(listed, 'Morning Roasters')
        self.assertContains(listed, 'renamed-owner')
        self.assertContains(listed, '14 Park Street')

    def test_edited_unicode_username_can_log_in(self):
        self.owner.set_password('keep-this-password')
        self.owner.save(update_fields=['password'])
        response = self.client.post(self.details_url, self._details_data(**{
            f'tenant-{self.tenant.pk}-username_{self.owner.tenantprofile.pk}': 'ｏｗｎｅｒ',
        }))
        self.assertRedirects(response, self.tenants_url)
        self.owner.refresh_from_db()
        self.assertEqual(self.owner.username, 'owner')
        for username in ('ｏｗｎｅｒ', 'owner'):
            with self.subTest(username=username):
                client = Client()
                response = client.post(reverse('login'), {
                    'username': username, 'password': 'keep-this-password',
                })
                self.assertEqual(response.status_code, 302)
                self.assertEqual(int(client.session['_auth_user_id']), self.owner.pk)

    def test_invalid_directory_edits_leave_the_tenant_unchanged(self):
        TenantInfo.objects.create(display_name='Taken Café')
        original_slug = self.tenant.slug
        profile = self.owner.tenantprofile
        prefix = f'tenant-{self.tenant.pk}'
        cases = (
            ({f'{prefix}-display_name': 'taken café'}, 'A business with this name already exists.'),
            ({f'{prefix}-display_name': '   '}, 'This field is required.'),
            ({f'{prefix}-username_{profile.pk}': 'master'}, 'A user with that username already exists.'),
            ({f'{prefix}-username_{profile.pk}': 'ｍａｓｔｅｒ'}, 'A user with that username already exists.'),
            ({f'{prefix}-username_{profile.pk}': ''}, 'This field is required.'),
            ({f'{prefix}-address': 'y' * 501}, 'Address must be 500 characters or fewer.'),
        )
        for overrides, message in cases:
            with self.subTest(message=message):
                response = self.client.post(self.details_url, self._details_data(**overrides))
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, message, status_code=400)
                self.assertContains(response, 'id="edit-tenant" open', status_code=400)
                self.assertContains(response, 'Tenant Details Not Saved', status_code=400)
                self.tenant.refresh_from_db()
                self.owner.refresh_from_db()
                self.assertEqual(self.tenant.display_name, 'Existing Café')
                self.assertEqual(self.tenant.address, '')
                self.assertEqual(self.tenant.slug, original_slug)
                self.assertEqual(self.owner.username, 'existing-owner')

    def test_edit_can_exchange_usernames_and_cannot_rename_another_tenants_user(self):
        second = get_user_model().objects.create_user(
            username='second-owner', email='second@example.org', password='second-password',
        )
        second_profile = TenantProfile.objects.create(user=second, tenant=self.tenant)
        other = TenantInfo.objects.create(display_name='Other Café')
        other_user = get_user_model().objects.create_user(username='other-owner')
        other_profile = TenantProfile.objects.create(user=other_user, tenant=other)
        prefix = f'tenant-{self.tenant.pk}'
        response = self.client.post(self.details_url, {
            f'{prefix}-display_name': self.tenant.display_name,
            f'{prefix}-address': self.tenant.address,
            f'{prefix}-username_{self.owner.tenantprofile.pk}': 'second-owner',
            f'{prefix}-username_{second_profile.pk}': 'existing-owner',
            f'{prefix}-username_{other_profile.pk}': 'stolen-owner',
        })
        self.assertRedirects(response, self.tenants_url)
        self.owner.refresh_from_db()
        second.refresh_from_db()
        other_user.refresh_from_db()
        self.assertEqual(self.owner.username, 'second-owner')
        self.assertEqual(second.username, 'existing-owner')
        self.assertEqual(second.email, 'second@example.org')
        self.assertTrue(second.check_password('second-password'))
        self.assertEqual(other_user.username, 'other-owner')
        self.assertEqual(TenantProfile.objects.get(pk=other_profile.pk).tenant_id, other.pk)

    def test_two_logins_cannot_share_a_username(self):
        second = get_user_model().objects.create_user(username='second-owner')
        second_profile = TenantProfile.objects.create(user=second, tenant=self.tenant)
        prefix = f'tenant-{self.tenant.pk}'
        response = self.client.post(self.details_url, {
            f'{prefix}-display_name': self.tenant.display_name,
            f'{prefix}-address': '',
            f'{prefix}-username_{self.owner.tenantprofile.pk}': 'shared-owner',
            f'{prefix}-username_{second_profile.pk}': 'shared-owner',
        })
        self.assertContains(response, 'A user with that username already exists.', status_code=400)
        self.owner.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(self.owner.username, 'existing-owner')
        self.assertEqual(second.username, 'second-owner')

    def test_tenant_without_a_login_can_still_edit_name_and_address(self):
        lonely = TenantInfo.objects.create(display_name='Lonely Café', address='Old lane')
        self.assertContains(
            self.client.get(f'{self.tenants_url}?edit={lonely.pk}'),
            'No login account is linked to this tenant.',
        )
        prefix = f'tenant-{lonely.pk}'
        original_slug = lonely.slug
        response = self.client.post(reverse('update_tenant_details', args=[lonely.pk]), {
            f'{prefix}-display_name': 'Lonely Roasters',
            f'{prefix}-address': '',
        })
        self.assertRedirects(response, self.tenants_url)
        lonely.refresh_from_db()
        self.assertEqual(lonely.display_name, 'Lonely Roasters')
        self.assertEqual(lonely.address, '')
        self.assertEqual(lonely.slug, original_slug)

    def test_saving_unchanged_details_is_a_successful_noop(self):
        with self.assertLogs('users.views', level='INFO') as logs:
            response = self.client.post(self.details_url, self._details_data())
        self.assertRedirects(response, self.tenants_url)
        self.assertIn(
            f'user_id={self.master.pk} tenant_id={self.tenant.pk} changed=none',
            '\n'.join(logs.output),
        )

    @override_settings(MIDDLEWARE=[
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.middleware.csrf.CsrfViewMiddleware',
        'django.contrib.auth.middleware.AuthenticationMiddleware',
        'django.contrib.messages.middleware.MessageMiddleware',
    ])
    def test_mutations_require_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.master)
        self.assertEqual(client.post(self.create_url, self.data).status_code, 403)
        self.assertEqual(client.post(self.active_url, {'is_active': 'false'}).status_code, 403)
        self.assertEqual(client.post(self.details_url, self._details_data()).status_code, 403)
        client.get(self.tenants_url)
        token = client.cookies['csrftoken'].value
        self.assertRedirects(client.post(self.active_url, {'is_active': 'false', 'csrfmiddlewaretoken': token}), self.tenants_url)


@override_settings(SIGNUP_ALERT_EMAIL='', LEGACY_TENANT_SYNC_ENABLED=False)
class TenantAddressSettingsTests(TestCase):
    def setUp(self):
        self.tenant = TenantInfo.objects.create(
            display_name='Settings Café', approval_status='APPROVED', address='Old address',
        )
        self.owner = get_user_model().objects.create_user(username='settings-owner')
        TenantProfile.objects.create(user=self.owner, tenant=self.tenant)
        self.client.force_login(self.owner)
        self.url = reverse('tenant:tenant_settings')

    def _location_data(self, **overrides):
        from tests.integration.test_site_location import FORM_DATA
        return {**FORM_DATA, **overrides}

    def test_settings_shows_and_updates_address(self):
        from tests.integration.test_site_location import CITY
        response = self.client.get(self.url)
        self.assertContains(response, 'name="street_address_1"')
        self.assertContains(response, 'Old address')
        self.assertContains(response, 'name="postal_code"')
        self.assertNotContains(response, 'value="None"')
        with patch('users.site_location.get_city', return_value=CITY), patch('users.site_location.valid_postal_code', return_value=True):
            response = self.client.post(self.url, self._location_data(street_address_1='  New street  '))
        self.assertRedirects(response, f'{self.url}?tab=contact')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.street_address_1, 'New street')
        self.assertEqual(self.tenant.address, 'New street, Kolkata, West Bengal, India, 700016')
        self.assertContains(self.client.get(self.url), 'New street')

    def test_settings_tabs_group_sections_and_reopen_the_submitted_tab(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'aria-label="Settings"')
        self.assertNotContains(response, '<h2>Tenant Settings</h2>')
        content = response.content.decode()
        checkout = content.split('id="settings-panel-checkout"', 1)[1].split('id="settings-panel-hours"', 1)[0]
        hours = content.split('id="settings-panel-hours"', 1)[1].split('id="settings-panel-contact"', 1)[0]
        contact = content.split('id="settings-panel-contact"', 1)[1].split('id="settings-panel-integrations"', 1)[0]
        integrations = content.split('id="settings-panel-integrations"', 1)[1]
        self.assertIn('name="modes"', checkout)
        self.assertNotIn('name="timezone"', checkout)
        self.assertIn('name="timezone"', hours)
        self.assertIn('name="hours_0"', hours)
        self.assertIn('name="street_address_1"', contact)
        self.assertNotIn('name="telegram_bot_token"', contact)
        self.assertIn('name="telegram_bot_token"', integrations)
        self.assertNotContains(response, 'name="telegram_chat_id"')
        self.assertIn('Paste your token from BotFather. Saving registers the Telegram webhook.', integrations)
        self.assertNotIn('name="geocoding_provider"', integrations)
        self.assertNotIn('name="geocoding_provider"', contact)
        self.assertIn('/commerce/settings/', checkout)
        self.assertNotIn('/commerce/settings/', integrations)
        self.assertContains(response, '>Opening hours</a>')
        self.assertContains(response, '>Business details</a>')
        self.assertIn('generate-token-button', integrations)
        self.assertIn(' hidden', hours)
        hours_page = self.client.get(f'{self.url}?tab=hours')
        self.assertContains(hours_page, 'aria-controls="settings-panel-hours" aria-selected="true"')
        self.assertNotContains(
            hours_page,
            'id="settings-panel-hours" class="settings-panel" role="tabpanel" aria-labelledby="settings-tab-hours" hidden',
        )
        invalid = self.client.post(self.url, {
            'section': 'checkout', 'settings_tab': 'hours', 'modes': ['pickup'],
            'timezone': 'Not/AZone', 'always_open': 'on', 'pickup_payment_methods': ['cash'],
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertContains(invalid, 'aria-controls="settings-panel-hours" aria-selected="true"', status_code=400)

    def test_checkout_errors_reveal_the_affected_tab_and_link_to_all_fields(self):
        from orders.models import CheckoutSettings
        data = {
            'section': 'checkout', 'modes': ['pickup'], 'timezone': 'Asia/Kolkata',
            'always_open': 'on', 'pickup_payment_methods': ['cash'],
        }
        for submitted_tab, changes, error_tab, field in (
            ('hours', {'pickup_fee': '0.001'}, 'checkout', 'pickup_fee'),
            ('checkout', {'timezone': ''}, 'hours', 'timezone'),
        ):
            with self.subTest(submitted_tab=submitted_tab):
                response = self.client.post(self.url, {**data, 'settings_tab': submitted_tab, **changes})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.context['active_tab'], error_tab)
                self.assertContains(response, f'href="#id_{field}_group" data-settings-error="{error_tab}"', status_code=400)
        both = self.client.post(self.url, {**data, 'timezone': '', 'pickup_fee': '0.001'})
        self.assertEqual(both.context['active_tab'], 'checkout')
        self.assertContains(both, 'data-settings-error="checkout"', status_code=400)
        self.assertContains(both, 'data-settings-error="hours"', status_code=400)
        self.assertFalse(CheckoutSettings.objects.filter(tenant=self.tenant).exists())

    @override_settings(PUBLIC_URL='https://example.org')
    def test_contact_and_telegram_saves_preserve_other_sections(self):
        self.tenant.telegram_chat_id = 'old-chat'
        self.tenant.telegram_bot_token = 'existing-token'
        self.tenant.save()
        from tests.integration.test_site_location import CITY
        with patch('users.views.requests.post') as webhook, self.captureOnCommitCallbacks(execute=True), patch('users.site_location.get_city', return_value=CITY), patch('users.site_location.valid_postal_code', return_value=True):
            response = self.client.post(self.url, self._location_data(
                street_address_1='New address', whatsapp_number='+15551234567', telegram_bot_token='ignored',
            ))
        self.assertRedirects(response, f'{self.url}?tab=contact')
        webhook.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.telegram_bot_token, 'existing-token')
        self.assertEqual(self.tenant.telegram_chat_id, 'old-chat')
        self.assertIsNone(self.tenant.whatsapp_number)
        response = self.client.post(self.url, {
            'section': 'whatsapp', 'whatsapp_number': '+15551234567',
        })
        self.assertRedirects(response, f'{self.url}?tab=contact')

        with patch('users.views.requests.post') as webhook, self.captureOnCommitCallbacks(execute=True):
            webhook.return_value.ok = True
            webhook.return_value.json.return_value = {'ok': True}
            response = self.client.post(self.url, {
                'section': 'integrations',
                'telegram_bot_token': 'new-token', 'address': 'ignored',
            })
        self.assertRedirects(response, f'{self.url}?tab=integrations')
        webhook.assert_called_once()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.address, 'New address, Kolkata, West Bengal, India, 700016')
        self.assertEqual(self.tenant.whatsapp_number, '+15551234567')
        self.assertEqual(self.tenant.telegram_chat_id, 'old-chat')
        self.assertEqual(self.tenant.telegram_bot_token, 'new-token')

        response = self.client.post(self.url, {'section': 'integrations', 'telegram_chat_id': '', 'telegram_bot_token': ''})
        self.assertRedirects(response, f'{self.url}?tab=integrations')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.telegram_bot_token, '')
        self.assertEqual(self.tenant.telegram_chat_id, 'old-chat')
        self.assertEqual(self.tenant.address, 'New address, Kolkata, West Bengal, India, 700016')

    def test_legacy_blank_and_overlong_addresses_cannot_bypass_validation(self):
        for address in ('   ', 'y' * 501):
            response = self.client.post(self.url, {'address': address, 'telegram_bot_token': 'changed'})
            self.assertEqual(response.status_code, 400)
            self.assertIn('street_address_1', response.context['location_form'].errors)
            self.tenant.refresh_from_db()
            self.assertEqual(self.tenant.address, 'Old address')
            self.assertIsNone(self.tenant.telegram_bot_token)

    def test_retired_delivery_geocoding_setting_is_not_offered_or_accepted(self):
        page = self.client.get(f'{self.url}?tab=integrations')
        self.assertNotContains(page, 'name="geocoding_provider"')
        for provider in ('google', 'openstreetmap'):
            rejected = self.client.post(self.url, {'section': 'geocoding', 'geocoding_provider': provider})
            self.assertEqual(rejected.status_code, 400)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.geocoding_provider, 'google')
        self.assertEqual(self.tenant.address, 'Old address')

    def test_rename_keeps_the_public_link(self):
        original_slug = self.tenant.slug
        self.assertEqual(original_slug, 'settings-cafe')
        response = self.client.get(self.url)
        self.assertContains(response, 'name="display_name"')
        self.assertContains(response, f'<strong>{original_slug}</strong>')
        self.assertNotContains(response, 'name="slug"')
        with self.assertLogs('users.views', level='INFO') as logs:
            response = self.client.post(self.url, {
                'section': 'profile', 'display_name': '  Morning Roasters  ', 'slug': 'hijacked',
            })
        self.assertRedirects(response, f'{self.url}?tab=contact')
        self.assertIn(f'user_id={self.owner.pk} tenant_id={self.tenant.pk}', '\n'.join(logs.output))
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.display_name, 'Morning Roasters')
        self.assertEqual(self.tenant.slug, original_slug)
        self.assertEqual(self.tenant.address, 'Old address')
        renamed = self.client.get(f'{self.url}?tab=contact')
        self.assertContains(renamed, 'Morning Roasters')
        self.assertContains(renamed, original_slug)

        TenantInfo.objects.create(display_name='Taken Café')
        rejected = self.client.post(self.url, {'section': 'profile', 'display_name': 'taken café'})
        self.assertContains(rejected, 'A business with this name already exists.', status_code=400)
        blank = self.client.post(self.url, {'section': 'profile', 'display_name': '   '})
        self.assertContains(blank, 'This field is required.', status_code=400)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.display_name, 'Morning Roasters')
        self.assertEqual(self.tenant.slug, original_slug)
