"""Signup feedback through the real form, view, and rendered templates."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from chatbot_core.models import TenantInfo, TenantRuntimeConfiguration
from users.forms import SignUpForm
from users.models import TenantProfile


@override_settings(SIGNUP_ALERT_EMAIL='', LEGACY_TENANT_SYNC_ENABLED=False)
class SignupFeedbackTests(TestCase):
    def setUp(self):
        self.data = {
            'username': 'cafe-owner', 'email': 'owner@example.org',
            'password': 'test-only-owner-password',
            'password2': 'test-only-owner-password',
            'business_name': 'Signup Café', 'business_type': 'cafe',
        }

    def test_initial_form_has_no_failure_message(self):
        response = self.client.get(reverse('signup'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="signup-errors"')

    @override_settings(SIGNUP_ALERT_EMAIL='master@example.org',
                       EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_public_signup_alert_and_channel_restrictions(self):
        from django.core import mail
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse('signup'), {
                **self.data, 'whatsapp_number': '+15551234567', 'telegram_chat_id': '12345',
            })
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].subject.startswith('New signup:'))
        self.assertIn('A new user signed up.', mail.outbox[0].body)
        self.assertIn('Address: not provided', mail.outbox[0].body)
        tenant = TenantInfo.objects.get(display_name=self.data['business_name'])
        self.assertFalse(tenant.whatsapp_number)
        self.assertFalse(tenant.telegram_chat_id)
        self.assertEqual(tenant.address, '')

    def test_invalid_fields_show_summary_and_preserve_non_password_input(self):
        for field, value, error in (
            ('email', 'not-an-email', 'Enter a valid email address.'),
            ('password2', 'different-password', 'Passwords do not match.'),
            ('business_name', '', 'This field is required.'),
            ('business_type', 'retail', 'Select a valid choice.'),
        ):
            with self.subTest(field=field):
                response = self.client.post(reverse('signup'), {**self.data, field: value})
                self.assertContains(response, 'id="signup-errors"')
                self.assertContains(response, 'Your account was not created.')
                self.assertContains(response, error)
                self.assertContains(response, f'href="#id_{field}"')
                self.assertContains(response, 'value="cafe-owner"')
                self.assertNotContains(response, self.data['password'])
                self.assertFalse(get_user_model().objects.exists())
                self.assertFalse(TenantInfo.objects.exists())

    def test_duplicate_username_and_business_show_actionable_errors(self):
        get_user_model().objects.create_user(username=self.data['username'])
        TenantInfo.objects.create(display_name=self.data['business_name'])
        response = self.client.post(reverse('signup'), self.data)
        self.assertContains(response, 'id="signup-errors"')
        self.assertContains(response, 'A user with that username already exists.')
        self.assertContains(response, 'A business with this name already exists.')
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(TenantInfo.objects.count(), 1)
        self.assertFalse(TenantProfile.objects.exists())

    def test_form_wide_errors_are_visible(self):
        with patch.object(SignUpForm, 'clean', side_effect=ValidationError('Please review your registration.')):
            response = self.client.post(reverse('signup'), self.data)
        self.assertContains(response, 'id="signup-errors"')
        self.assertContains(response, 'Please review your registration.')
        self.assertFalse(get_user_model().objects.exists())

    def test_success_signs_in_owner_and_confirms_pending_approval(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('signup'), self.data, follow=True)
        self.assertEqual(response.redirect_chain, [
            (reverse('dashboard'), 302), (reverse('pending_review'), 302),
        ])
        self.assertContains(response, 'Your account has been created successfully.')
        self.assertContains(response, 'Your café is awaiting administrator approval.')
        self.assertNotContains(response, 'id="signup-errors"')
        owner = get_user_model().objects.get(username=self.data['username'])
        self.assertTrue(owner.check_password(self.data['password']))
        self.assertEqual(int(self.client.session['_auth_user_id']), owner.pk)
        tenant = TenantProfile.objects.get(user=owner).tenant
        self.assertEqual(tenant.approval_status, 'PENDING')
        self.assertEqual(tenant.slug, 'signup-cafe')
        self.assertTrue(TenantRuntimeConfiguration.objects.filter(tenant=tenant).exists())

    def test_address_is_optional_and_stored_when_provided(self):
        response = self.client.get(reverse('signup'))
        self.assertContains(response, 'name="address"')
        self.assertContains(response, 'Optional.')
        address = '221B Baker Street\nLondon NW1'
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(reverse('signup'), {**self.data, 'address': f'  {address}  '})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TenantInfo.objects.get().address, address)

    def test_public_link_can_be_chosen_and_rejects_a_taken_or_empty_value(self):
        response = self.client.get(reverse('signup'))
        self.assertContains(response, 'name="slug"')
        self.assertContains(response, 'This link stays the same if the business is renamed.')
        with self.captureOnCommitCallbacks(execute=True):
            chosen = self.client.post(reverse('signup'), {**self.data, 'slug': ' My Public Link '})
        self.assertEqual(chosen.status_code, 302)
        self.assertEqual(TenantInfo.objects.get().slug, 'my-public-link')

        taken = self.client.post(reverse('signup'), {
            **self.data, 'username': 'other-owner', 'slug': 'my-public-link',
        })
        self.assertContains(taken, 'This public link is already in use.')
        self.assertEqual(TenantInfo.objects.count(), 1)

        punctuation = self.client.post(reverse('signup'), {**self.data, 'username': 'punctuation-owner', 'business_name': '!!!'})
        self.assertContains(punctuation, 'Enter a business name that can be used in a web address.')
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_matching_names_receive_distinct_public_links(self):
        first = TenantInfo.objects.create(display_name='Signup Café')
        second = TenantInfo.objects.create(display_name='Signup Cafe')
        self.assertEqual(first.slug, 'signup-cafe')
        self.assertEqual(second.slug, 'signup-cafe-2')
        second.display_name = 'Morning Roasters'
        second.slug = ''
        with self.assertRaises(ValidationError):
            second.save()
        second.refresh_from_db()
        self.assertEqual(second.slug, 'signup-cafe-2')
        self.assertEqual(second.display_name, 'Signup Cafe')

    def test_address_too_long_does_not_create_an_account(self):
        response = self.client.post(reverse('signup'), {**self.data, 'address': 'x' * 501})
        self.assertContains(response, 'id="signup-errors"')
        self.assertContains(response, 'Ensure this value has at most 500 characters (it has 501).')
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(TenantInfo.objects.exists())
