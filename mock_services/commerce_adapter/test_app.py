import json
import tempfile
import unittest
import uuid
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from .demo import DemoClient, run_demo
from .fixtures import command, identifier, payment_command
from .provider import FakeProvider, webhook_signature
from .storage import Store, encode
from .worker import Worker


class ReferenceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.account = identifier('payment-connection')
        self.store = Store(self.path / 'adapter.sqlite3', self.account)
        self.provider = FakeProvider(self.path / 'provider.sqlite3', self.account)
        self.client = DemoClient()
        self.worker = Worker(self.store, self.provider, self.client, 'webhook-secret')
        self.addCleanup(lambda: self.store.close())
        self.addCleanup(self.provider.close)

    def restart(self):
        self.store.close()
        self.store = Store(self.path / 'adapter.sqlite3', self.account)
        self.worker = Worker(self.store, self.provider, self.client, 'webhook-secret')

    def count_resources(self):
        return self.provider.db.execute('SELECT COUNT(*) FROM resources').fetchone()[0]

    def test_duplicate_delivery_new_lease_and_payload_conflict(self):
        create = payment_command()
        self.worker.handle(create)
        self.restart()
        redelivery = {**create, 'lease_token': str(uuid.uuid4()), 'attempt': 2}
        self.worker.handle(redelivery)
        self.worker.flush_acks()
        receipt = self.store.db.execute('SELECT * FROM command_receipt').fetchone()
        self.assertEqual(json.loads(receipt['delivery'])['lease_token'], redelivery['lease_token'])
        self.assertEqual(receipt['ack_state'], 'sent')
        self.assertEqual(self.count_resources(), 1)
        self.assertEqual(len(self.store.summary()['outbox']), 1)
        changed = deepcopy(create)
        changed['data']['amount_minor'] += 1
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.worker.handle(changed)
        self.assertEqual(self.count_resources(), 1)

    def test_crash_after_provider_commit_recovers_without_duplicate(self):
        create = payment_command()
        with patch.object(self.worker, 'observe', side_effect=RuntimeError('power lost')):
            with self.assertRaises(RuntimeError):
                self.worker.handle(create)
        self.assertEqual(self.store.summary()['receipts'][0]['state'], 'processing')
        self.assertEqual(self.store.summary()['outbox'], [])
        self.assertEqual(self.count_resources(), 1)
        self.restart()
        self.worker.tick()
        self.assertEqual(self.count_resources(), 1)
        self.assertEqual(self.store.summary()['receipts'][0]['state'], 'done')
        self.assertEqual(self.store.summary()['outbox'][0]['state'], 'sent')

    def test_expired_crash_receipt_waits_for_a_fresh_lease(self):
        create = payment_command()
        with patch.object(self.provider, 'create', side_effect=RuntimeError('crash before IO')):
            with self.assertRaises(RuntimeError):
                self.worker.handle(create)
        self.worker.clock = lambda: 9999999999
        self.worker.tick()
        self.assertEqual(self.count_resources(), 0)

    def test_timeout_then_restart_reconciliation_uses_original_key(self):
        create = payment_command()
        self.worker.handle(create, timeout_after_commit=True)
        self.worker.flush_acks()
        self.assertEqual(self.client.acks[-1][1], 'unknown')
        self.restart()
        reconcile = command('payment.reconcile', dict(payment_id=create['data']['payment_id'], external_id=''))
        with patch.object(self.provider, 'create', side_effect=AssertionError('Must not recreate')):
            self.worker.handle(reconcile)
        self.assertEqual(self.count_resources(), 1)
        self.assertEqual(self.store.summary()['receipts'][-1]['outcome'], 'succeeded')
        self.assertIsNone(self.provider.lookup(key=reconcile['idempotency_key']))

    def test_missing_reconciliation_reference_is_unknown_without_side_effect(self):
        self.worker.handle(command('payment.reconcile', dict(payment_id=identifier('missing'), external_id='')))
        self.assertEqual(self.store.summary()['receipts'][0]['outcome'], 'unknown')
        self.assertEqual(self.count_resources(), 0)

    def test_event_response_loss_retries_same_payload_after_restart(self):
        self.worker.handle(payment_command())
        payload = self.store.db.execute('SELECT payload FROM event_outbox').fetchone()[0]
        self.client.lose_event_response = True
        self.worker.flush_events()
        self.assertEqual(self.store.summary()['outbox'][0]['state'], 'pending')
        self.restart()
        with self.store.db:
            self.store.db.execute('UPDATE event_outbox SET next_attempt=0')
        self.worker.flush_events()
        self.assertEqual(self.store.db.execute('SELECT payload FROM event_outbox').fetchone()[0], payload)
        self.assertEqual(len(self.client.events), 1)
        self.assertEqual(self.store.summary()['outbox'][0]['attempts'], 2)
        self.assertEqual(self.store.summary()['outbox'][0]['state'], 'sent')

    def test_failed_engine_processing_and_permanent_http_errors_are_not_success(self):
        self.worker.handle(payment_command())
        with patch.object(self.client, 'send_event', return_value={'status': 'failed'}):
            self.worker.flush_events()
        self.assertEqual(self.store.summary()['outbox'][0]['state'], 'pending')
        with self.store.db:
            self.store.db.execute('UPDATE event_outbox SET next_attempt=0')
        with patch.object(self.client, 'send_event', side_effect=HTTPError('https://example.test', 400, 'bad', {}, None)):
            self.worker.flush_events()
        self.assertEqual(self.store.summary()['outbox'][0]['state'], 'parked')

    def test_acknowledgement_response_loss_is_durable(self):
        self.worker.handle(payment_command())
        with patch.object(self.client, 'acknowledge', side_effect=TimeoutError):
            self.worker.flush_acks()
        self.restart()
        with self.store.db:
            self.store.db.execute('UPDATE command_receipt SET ack_next=0')
        self.worker.flush_acks()
        self.assertEqual(self.store.summary()['receipts'][0]['ack_state'], 'sent')
        self.assertEqual(self.count_resources(), 1)

    def test_webhook_verification_deduplication_and_authoritative_fetch(self):
        create = payment_command()
        self.worker.handle(create)
        body, stamp, signature = self.provider.signed_capture(create['data']['payment_id'], 'webhook-secret')
        for bad_stamp, bad_signature in ((stamp, '0' * 64), ('0', webhook_signature('webhook-secret', '0', body))):
            with self.assertRaises(ValueError):
                self.worker.webhook(body, bad_stamp, bad_signature)
        self.assertEqual(self.store.summary()['webhooks'], [])
        self.worker.webhook(body, stamp, signature)
        self.restart()
        self.worker.webhook(body, stamp, signature)
        self.assertEqual(len(self.store.summary()['webhooks']), 1)
        self.assertEqual(len(self.store.summary()['outbox']), 2)
        notification = json.loads(body)
        notification['account'] = 'other-account'
        foreign = encode(notification).encode()
        with self.assertRaisesRegex(ValueError, 'another account'):
            self.worker.webhook(foreign, stamp, webhook_signature('webhook-secret', stamp, foreign))

    def test_observation_and_webhook_receipt_commit_atomically(self):
        create = payment_command()
        self.worker.handle(create)
        body, stamp, signature = self.provider.signed_capture(create['data']['payment_id'], 'webhook-secret')
        observe = self.worker.observe
        def fail_after_observe(value):
            observe(value)
            raise RuntimeError('power lost')
        with patch.object(self.worker, 'observe', side_effect=fail_after_observe):
            with self.assertRaises(RuntimeError):
                self.worker.webhook(body, stamp, signature)
        self.assertEqual(len(self.store.summary()['outbox']), 1)
        self.assertEqual(self.store.summary()['webhooks'], [])
        self.worker.webhook(body, stamp, signature)
        self.assertEqual(len(self.store.summary()['outbox']), 2)

    def test_unsupported_capability_never_calls_provider(self):
        self.worker.handle(command('payment.refund', dict(payment_id=identifier('payment'), external_id='fake', currency='EUR', exponent=2, target_refunded_minor=1250)))
        self.assertEqual(self.count_resources(), 0)
        self.assertEqual(self.store.summary()['receipts'][0]['outcome'], 'failed')

    def test_unsupported_envelope_never_calls_provider(self):
        create = payment_command()
        create['schema_version'] = 2
        self.worker.handle(create)
        self.assertEqual(self.count_resources(), 0)
        self.assertEqual(self.store.summary()['receipts'][0]['error_code'], 'unsupported_command_version')

    def test_full_offline_walkthrough(self):
        root = run_demo(self.path)
        self.assertEqual(len(list(root.glob('*/summary.json'))), 3)


if __name__ == '__main__':
    unittest.main()
