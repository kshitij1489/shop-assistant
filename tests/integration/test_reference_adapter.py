"""Exercise the separate reference adapter against real signed commerce endpoints."""
import json
import tempfile
from datetime import timedelta
from pathlib import Path

from django.test import TestCase, override_settings
from django.utils import timezone

from commerce.command_schemas import ClaimResponse, command_schema
from commerce.models import Command, Payment
from commerce.queue import reconcile
from commerce.services import cancel_order
from mock_services.commerce_adapter.provider import FakeProvider
from mock_services.commerce_adapter.storage import Store
from mock_services.commerce_adapter.worker import Worker
from tests.support.commerce import EngineClient, Fixtures
from tests.support.paths import REPOSITORY_ROOT


@override_settings(ROOT_URLCONF='commerce.urls', COMMERCE_ADAPTER_SECRETS={'test-key': 'test-adapter-secret'})
class ReferenceEngineTests(Fixtures, TestCase):
    def setUp(self):
        self.seed()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workers = {}
        for role, connection in (('payment', self.gateway), ('pos', self.pos)):
            path = Path(self.tmp.name)
            store = Store(path / (role + '.sqlite3'), str(connection.pk))
            provider = FakeProvider(path / 'provider.sqlite3', str(connection.pk))
            self.workers[role] = Worker(store, provider, EngineClient(self.client, connection), 'webhook-secret')
            self.addCleanup(store.close)
            self.addCleanup(provider.close)

    def test_confirmation_payment_pos_acceptance_and_duplicate_event(self):
        record = self.accept()
        payment, pos = self.workers['payment'], self.workers['pos']
        payment.tick()
        record.refresh_from_db()
        self.assertEqual(record.state, 'awaiting_payment')
        self.assertEqual(record.payments.get().status, 'pending')
        body, stamp, signed = payment.provider.signed_capture(str(record.payments.get().pk), 'webhook-secret')
        payment.webhook(body, stamp, signed)
        payment.webhook(body, stamp, signed)
        payment.tick()
        record.refresh_from_db()
        self.assertEqual(record.state, 'confirmed')
        self.assertEqual(record.order.payment_status, 'paid')
        pos.tick()
        record.refresh_from_db()
        self.assertEqual(record.pos_state, 'accepted')
        self.assertEqual(record.commands.filter(status='succeeded').count(), 2)
        self.assertEqual(record.reservations.get().state, 'consumed')
        # Re-deliver exact events through real authentication and engine deduplication.
        for worker in (payment, pos):
            for row in worker.db.execute('SELECT payload FROM event_outbox'):
                self.assertEqual(worker.client.send_event(json.loads(row[0]))['status'], 'processed')
        self.assertEqual(record.commands.filter(kind='order.submit').count(), 1)

    def test_real_reconciliation_commands_after_lost_provider_responses(self):
        record = self.accept()
        payment, pos = self.workers['payment'], self.workers['pos']
        create = payment.client.claim()[0]
        payment.handle(create, timeout_after_commit=True)
        payment.flush_acks()
        self.assertEqual(Command.objects.get(pk=create['command_id']).status, 'unknown')
        Payment.objects.filter(accepted_order=record).update(updated_at=timezone.now() - timedelta(minutes=6))
        reconcile()
        payment.tick()
        self.assertEqual(record.payments.get().status, 'pending')
        body, stamp, signed = payment.provider.signed_capture(str(record.payments.get().pk), 'webhook-secret')
        payment.webhook(body, stamp, signed)
        payment.flush_events()
        submit = pos.client.claim()[0]
        pos.handle(submit, timeout_after_commit=True)
        pos.flush_acks()
        type(record).objects.filter(pk=record.pk).update(created_at=timezone.now() - timedelta(minutes=6))
        reconcile()
        pos.tick()
        record.refresh_from_db()
        self.assertEqual(record.pos_state, 'accepted')
        self.assertEqual(pos.provider.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0], 2)

    def test_all_published_command_variants_and_schema_endpoint(self):
        record = self.accept()
        payment = self.workers['payment']
        create = payment.client.claim()[0]
        command_schema.validate_python(create)
        self.gateway.capabilities.append('payment.refund')
        self.gateway.save()
        cancel_order(record.pk)
        from commerce.events import receive
        receive(self.gateway, self.event(record))
        refund = payment.client.claim()[0]
        self.assertEqual(refund['type'], 'payment.refund')
        command_schema.validate_python(refund)
        schemas = payment.client.request('GET', 'schema/')
        self.assertEqual(schemas['command'], command_schema.json_schema())
        self.assertEqual(schemas['claim_response'], ClaimResponse.model_json_schema())
        contracts = REPOSITORY_ROOT / 'commerce' / 'contracts'
        for name in ('command', 'claim_response'):
            self.assertEqual(json.loads((contracts / (name + '.schema.json')).read_text()), schemas[name])
