from django.conf import settings
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.contrib.auth.models import User
from django.db import close_old_connections
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse

from commerce.models import Command, Inbox, ReconciliationIssue
from commerce.services import issue, pos_submit
from tests.support.commerce import Fixtures
from users.models import TenantProfile


@override_settings(ROOT_URLCONF='tests.support.urls')
class CommerceOperationsTests(Fixtures, TestCase):
    def setUp(self):
        self.seed()
        self.user = User.objects.create_user(username='operator', password='test')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.record = self.accept()
        self.issue = issue(self.record, 'adapter_unknown', command_id=str(self.record.commands.get().pk))
        self.url = reverse('commerce:issue', args=[self.issue.pk])
        self.resolution = {
            'evidence': 'Verified merchant/test, reference pay-123: fully refunded; ticket INC-42.',
            'note': 'Provider observation delivered as event recovery-1; order abandoned, no restock.',
            'verified': 'on',
        }

    def test_investigation_exposes_original_keys_and_provider_identity(self):
        response = self.client.get(self.url)
        for value in (str(self.record.commands.get().pk), str(self.gateway.pk),
                      'merchant', str(self.record.payments.get().pk), self.record.snapshot_hash,
                      str(self.record.reservations.get().pk), 'Investigate before resolving'):
            self.assertContains(response, value)
        self.assertEqual(response.headers['Cache-Control'], 'max-age=0, no-cache, no-store, must-revalidate, private')

    def test_resolution_is_audited_without_business_side_effects(self):
        before_order = (self.record.state, self.record.pos_state, self.record.snapshot_hash)
        before_commands = list(Command.objects.values())
        before_payments = list(self.record.payments.values())
        before_holds = list(self.record.reservations.values())
        response = self.client.post(self.url, self.resolution)
        self.assertRedirects(response, self.url)
        self.issue.refresh_from_db()
        self.record.refresh_from_db()
        self.assertIsNotNone(self.issue.resolved_at)
        self.assertEqual(self.issue.resolved_by, f'user:{self.user.pk}:operator')
        self.assertEqual(self.issue.resolution_evidence, self.resolution['evidence'])
        self.assertEqual(self.issue.resolution_note, self.resolution['note'])
        self.assertEqual(before_order, (self.record.state, self.record.pos_state, self.record.snapshot_hash))
        self.assertEqual(before_commands, list(Command.objects.values()))
        self.assertEqual(before_payments, list(self.record.payments.values()))
        self.assertEqual(before_holds, list(self.record.reservations.values()))
        history = self.client.get(reverse('commerce:operations'), {'status': 'resolved'})
        self.assertContains(history, self.url)
        self.assertNotContains(self.client.get(reverse('commerce:operations')), self.url)

    def test_resolution_requires_evidence_disposition_and_verification(self):
        for field in self.resolution:
            with self.subTest(field=field):
                self.assertEqual(self.client.post(self.url, {**self.resolution, field: ''}).status_code, 400)
        self.assertEqual(self.client.post(self.url, {**self.resolution, 'evidence': '   '}).status_code, 400)
        self.issue.refresh_from_db()
        self.assertIsNone(self.issue.resolved_at)

    def test_existing_resolution_cannot_be_overwritten_and_recurrence_is_new(self):
        self.client.post(self.url, self.resolution)
        self.issue.refresh_from_db()
        original = self.issue.resolved_at
        self.assertEqual(self.client.post(self.url, {**self.resolution, 'note': 'overwrite'}).status_code, 400)
        self.issue.refresh_from_db()
        self.assertEqual(self.issue.resolution_note, self.resolution['note'])
        self.assertEqual(self.issue.resolved_at, original)
        recurrence = issue(self.record, 'adapter_unknown')
        self.assertNotEqual(recurrence.pk, self.issue.pk)
        self.assertIsNone(recurrence.resolved_at)

    def test_other_tenant_cannot_read_or_resolve_or_list_incident_data(self):
        other = type(self.tenant).objects.create(display_name='Other', approval_status='APPROVED')
        profile = self.user.tenantprofile
        profile.tenant = other
        profile.save()
        Command.objects.filter(accepted_order=self.record).update(status='unknown')
        Inbox.objects.create(connection=self.gateway, event_id='private-event', event_type='payment.updated',
                             payload={}, payload_hash='x', status='failed')
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, self.resolution).status_code, 404)
        response = self.client.get(reverse('commerce:operations'))
        for value in (self.url, str(self.record.commands.get().pk), 'private-event'):
            self.assertNotContains(response, value)
        self.issue.refresh_from_db()
        self.assertIsNone(self.issue.resolved_at)

    def test_parked_commands_and_exhausted_inbox_are_visible_without_issue(self):
        self.issue.delete()
        command = self.record.commands.get()
        command.status = 'unknown'
        command.save()
        Inbox.objects.create(connection=self.gateway, event_id='exhausted-event', event_type='payment.updated',
                             payload={}, payload_hash='x', status='failed', attempts=10, error='ValueError')
        response = self.client.get(reverse('commerce:operations'))
        self.assertContains(response, str(command.pk))
        self.assertContains(response, 'exhausted-event')
        self.assertContains(response, 'attempts 10')
        self.assertContains(response, 'One selected checkout location per tenant')

    @override_settings(MIDDLEWARE=[*settings.MIDDLEWARE, 'django.middleware.csrf.CsrfViewMiddleware'])
    def test_csrf_and_authentication_are_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, self.resolution).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, self.resolution).status_code, 302)

    def test_automatic_configuration_resolution_records_system_evidence(self):
        config_issue = issue(self.record, 'pos_unconfigured')
        pos_submit(self.record)
        config_issue.refresh_from_db()
        self.assertEqual(config_issue.resolved_by, 'system:pos_submit')
        self.assertIn('durably queued', config_issue.resolution_evidence)
        self.assertIn('acceptance', config_issue.resolution_note)

    def test_resolved_history_paginates(self):
        from django.utils import timezone
        ReconciliationIssue.objects.bulk_create([
            ReconciliationIssue(accepted_order=self.record, code=f'history_{number}', resolved_at=timezone.now())
            for number in range(51)
        ])
        response = self.client.get(reverse('commerce:operations'), {'status': 'resolved', 'page': 2})
        self.assertContains(response, '<a href="?status=resolved&amp;page=1">Previous issues</a>', html=True)


@override_settings(ROOT_URLCONF='tests.support.urls')
class ConcurrentResolutionTests(Fixtures, TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_two_operators_cannot_overwrite_each_others_resolution(self):
        self.seed()
        record = self.accept()
        incident = issue(record, 'adapter_unknown')
        clients = []
        for number in range(2):
            user = User.objects.create_user(username=f'operator-{number}')
            TenantProfile.objects.create(user=user, tenant=self.tenant)
            client = Client()
            client.force_login(user)
            clients.append(client)
        barrier = Barrier(2)

        def resolve(number):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                response = clients[number].post(reverse('commerce:issue', args=[incident.pk]), {
                    'evidence': f'Verified reference by operator {number}',
                    'note': f'Disposition {number}', 'verified': 'on',
                })
                return number, response.status_code
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(resolve, range(2)))
        self.assertCountEqual([status for _, status in results], [302, 400])
        winner = next(number for number, status in results if status == 302)
        incident.refresh_from_db()
        self.assertTrue(incident.resolved_by.endswith(f':operator-{winner}'))
        self.assertEqual(incident.resolution_note, f'Disposition {winner}')
