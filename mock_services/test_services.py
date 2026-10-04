"""Standard-library integration tests over real loopback HTTP and SQLite."""
import argparse
import copy
import json
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from mock_services.commerce_adapter.demo import DemoClient
from mock_services.commerce_adapter.fixtures import command, order_command, payment_command
from mock_services.commerce_adapter.storage import Store
from mock_services.commerce_adapter.worker import Worker
from .__main__ import sync_menu, validate_manifest
from .catalog import load_catalog
from .client import HTTPProvider, MockClient, refresh_payments
from .controls import CAPABILITIES, ProviderControls, Unsupported
from .server import make_server


class ServicesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)
        self.server = make_server(('127.0.0.1', 0), self.path / 'provider.sqlite3', load_catalog())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.client = MockClient('http://127.0.0.1:' + str(self.server.server_port))
        self.account = str(uuid.uuid4())
        self.provider = HTTPProvider(self.client, self.account, 'payment')

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def worker(self, role='payment'):
        account = self.account if role == 'payment' else str(uuid.uuid4())
        store = Store(self.path / (role + '.sqlite3'), account)
        self.addCleanup(store.close)
        return Worker(store, HTTPProvider(self.client, account, role), DemoClient(), '')

    def test_concurrent_idempotency_and_account_isolation(self):
        create = payment_command()
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: self.provider.create(create), range(48)))
        self.assertEqual(len({row['data']['external_id'] for row in results}), 1)
        self.assertEqual(len(self.client.request('GET', self.provider.path)['resources']), 1)
        other = HTTPProvider(self.client, str(uuid.uuid4()), 'payment')
        self.assertIsNone(other.lookup(key=create['idempotency_key']))
        self.assertNotEqual(other.create(create)['data']['external_id'], results[0]['data']['external_id'])
        changed = copy.deepcopy(create)
        changed['data']['amount_minor'] += 1
        with self.assertRaises(ValueError):
            self.provider.create(changed)

    def test_capture_and_pos_with_durable_outbox(self):
        payment = self.worker()
        payment.handle(payment_command())
        refresh_payments(payment, auto_capture=True)
        refresh_payments(payment, auto_capture=True)
        payment.client.lose_event_response = True
        payment.flush_events()
        with payment.db:
            payment.db.execute('UPDATE event_outbox SET next_attempt=0')
        payment.flush_events()
        self.assertEqual(len(payment.client.events), 2)
        captured = next(e['data'] for e in payment.client.events.values() if e['data']['status'] == 'captured')
        pos = self.worker('pos')
        submit = order_command(captured)
        pos.handle(submit)
        pos.handle(dict(submit, lease_token=str(uuid.uuid4()), attempt=2))
        pos.flush_events()
        self.assertEqual(len(pos.client.events), 1)
        self.assertEqual(next(iter(pos.client.events.values()))['data']['status'], 'accepted')
        self.assertTrue(all(r['state'] == 'sent' for r in payment.store.summary()['outbox']))

    def test_timeout_after_commit_reconciles_original_key(self):
        payment = self.worker()
        self.client.request('POST', '/admin/faults', {'payment': {'timeout_after_commit_next': 1}})
        create = payment_command()
        payment.handle(create)
        self.assertEqual(payment.store.summary()['receipts'][0]['outcome'], 'unknown')
        # Re-open adapter state while retaining the independently persisted provider state.
        payment.store.close()
        payment = self.worker()
        payment.handle(command('payment.reconcile', {'payment_id': create['data']['payment_id'], 'external_id': ''}))
        refresh_payments(payment, auto_capture=True)
        payment.flush_events()
        self.assertEqual(len(self.client.request('GET', self.provider.path)['resources']), 1)
        self.assertEqual(len(payment.client.events), 2)

    def test_pos_timeout_and_snapshot_integrity(self):
        pos = self.worker('pos')
        submit = order_command({'external_id': 'mock-paid'})
        self.client.request('POST', '/admin/faults', {'pos': {'timeout_after_commit_next': 1}})
        pos.handle(submit)
        self.assertEqual(pos.store.summary()['receipts'][0]['outcome'], 'unknown')
        pos.handle(command('order.reconcile', dict(accepted_order_id=submit['data']['accepted_order_id'],
            order_id=submit['data']['order_id'], original_command_id=submit['command_id'])))
        pos.flush_events()
        self.assertEqual(len(pos.client.events), 1)
        changed = copy.deepcopy(submit)
        changed['idempotency_key'] = str(uuid.uuid4())
        changed['data']['snapshot']['instructions'] = 'tampered'
        with self.assertRaises(ValueError):
            pos.provider.create(changed)

    def test_capture_is_idempotent_and_failure_is_terminal(self):
        create = payment_command()
        self.provider.create(create)
        payment_id = create['data']['payment_id']
        with ThreadPoolExecutor(max_workers=8) as pool:
            captures = list(pool.map(lambda _: self.provider.capture(payment_id), range(16)))
        self.assertEqual({c['data']['sequence'] for c in captures}, {2})
        self.assertEqual({c['data']['captured_minor'] for c in captures}, {1250})
        with self.assertRaises(HTTPError):
            self.client.request('POST', self.provider.path + '/' + payment_id + '/fail', {})

    def test_failed_payment_observed_without_capture(self):
        payment = self.worker()
        create = payment_command()
        payment.handle(create)
        self.client.request('POST', self.provider.path + '/' + create['data']['payment_id'] + '/fail', {})
        refresh_payments(payment, auto_capture=True)
        payment.flush_events()
        statuses = {e['data']['status'] for e in payment.client.events.values()}
        self.assertEqual(statuses, {'pending', 'failed'})

    def test_fault_before_commit_and_fault_reset(self):
        self.client.request('POST', '/admin/faults', {'payment': {'fail_next': 1}})
        with self.assertRaises(TimeoutError):
            self.provider.create(payment_command())
        self.assertEqual(self.client.request('GET', self.provider.path)['resources'], [])
        self.provider.create(payment_command())
        state = self.client.request('GET', '/admin/state')
        self.assertEqual(state['counters']['payment.fail_next'], 1)
        self.client.request('POST', '/admin/faults', {})
        self.assertEqual(self.client.request('GET', '/admin/state')['faults'], {})

    def test_account_scoped_faults_are_not_consumed_by_other_accounts(self):
        controls = ProviderControls(self.client)
        other = HTTPProvider(self.client, str(uuid.uuid4()), 'payment')
        create = payment_command()
        controls.configure_faults(self.account, {'payment': {'fail_next': 1}})
        # The other account keeps working and does not use up this account's fault.
        other.create(create)
        with self.assertRaises(TimeoutError):
            self.provider.create(create)
        self.assertEqual(self.client.request('GET', self.provider.path)['resources'], [])
        self.provider.create(create)
        state = controls.account_state(self.account)
        self.assertEqual(state['counters']['payment.fail_next'], 1)
        self.assertEqual(state['isolated_services'], ['payment'])
        self.assertEqual([p['status'] for p in state['resources']['payments']], ['pending'])
        self.assertEqual(self.client.request('GET', '/admin/state')['counters'].get('payment.fail_next', 0), 0)
        # A global rule applies only to accounts without their own rule for that service.
        self.client.request('POST', '/admin/faults', {'payment': {'fail_next': 1}})
        self.provider.create(create)
        with self.assertRaises(TimeoutError):
            other.create(create)
        controls.configure_faults(self.account, {})
        self.assertEqual(controls.account_state(self.account)['isolated_services'], [])

    def test_payment_controls_capture_with_expected_amount_and_timeout_creation(self):
        controls = ProviderControls(self.client)
        self.assertIn('payment_control.capture', CAPABILITIES)
        create = payment_command()
        self.provider.create(create)
        payment_id = create['data']['payment_id']
        with self.assertRaises(ValueError):
            controls.apply_payment_control(self.account, 'capture', payment_id=payment_id, amount_minor=1)
        with self.assertRaises(ValueError):
            controls.apply_payment_control(self.account, 'capture', payment_id=payment_id, amount_minor=1250, currency='INR')
        self.assertEqual(self.provider.lookup(key=create['idempotency_key'])['data']['status'], 'pending')
        captured = controls.apply_payment_control(self.account, 'capture', payment_id=payment_id, amount_minor=1250, currency='EUR')
        self.assertEqual((captured['data']['status'], captured['data']['captured_minor']), ('captured', 1250))
        with self.assertRaises(ValueError):
            controls.apply_payment_control(self.account, 'fail', payment_id=payment_id)
        with self.assertRaises(Unsupported):
            controls.apply_payment_control(self.account, 'restore_and_reconcile')
        controls.apply_payment_control(self.account, 'timeout_creation')
        second = payment_command()
        second.update(idempotency_key=str(uuid.uuid4()), command_id=str(uuid.uuid4()))
        second['data']['payment_id'] = str(uuid.uuid4())
        with self.assertRaises(TimeoutError):
            self.provider.create(second)
        # Committed before the lost response: reconciliation by key finds it pending.
        self.assertEqual(self.provider.lookup(key=second['idempotency_key'])['data']['status'], 'pending')
        self.assertEqual(len(self.client.request('GET', self.provider.path)['resources']), 2)

    def test_menu_exports_persist_sequence_and_match_test_data(self):
        generation = str(uuid.uuid4())
        path = '/v1/accounts/' + self.account + '/menu/snapshots'
        one = self.client.request('POST', path, {'source_generation': generation, 'after_sequence': 8})
        two = self.client.request('POST', path, {'source_generation': generation})
        self.assertEqual((one['sequence'], two['sequence']), (9, 10))
        self.assertEqual(len(one['items']), 26)
        self.assertEqual(one['items'][0]['variants'][0]['price'], '380.00')
        self.assertTrue(one['complete'])
        self.assertEqual(one['currency'], 'INR')
        # A new generation has an independent sequence.
        three = self.client.request('POST', path, {'source_generation': str(uuid.uuid4())})
        self.assertEqual(three['sequence'], 1)

    def test_menu_delivery_retry_preserves_exact_snapshot(self):
        generation = str(uuid.uuid4())
        manifest = dict(environment='test', connection_id=self.account, capabilities=['catalog.write'],
                        menu_source=dict(is_authority=True, generation=generation, sequence=0))
        payloads = []

        class Engine:
            def request(self, method, endpoint, payload=None):
                if method == 'GET':
                    return manifest
                payloads.append(payload)
                if len(payloads) == 1:
                    raise TimeoutError('Response lost after import')
                return {'status': 'unchanged'}

        args = argparse.Namespace(base_url='https://test.invalid/commerce', connection=self.account,
            state_dir=self.path, mock_url=self.client.base_url, timeout=5)
        with patch.dict('os.environ', {'COMMERCE_ADAPTER_SECRET': 'test'}), patch(
                'mock_services.__main__.AdapterClient', return_value=Engine()), patch('builtins.print'):
            with self.assertRaises(TimeoutError):
                sync_menu(args)
            sync_menu(args)
        self.assertEqual(payloads[0], payloads[1])
        self.assertFalse((self.path / (self.account + '-menu-pending.json')).exists())
        self.assertTrue((self.path / (self.account + '-menu-last.json')).exists())

    def test_manifest_rejects_live_and_unsupported_capabilities(self):
        manifest = dict(environment='test', connection_id=self.account, role='pos',
                        capabilities=['order.submit', 'order.reconcile', 'catalog.write'])
        validate_manifest(manifest, self.account, 'pos')
        for changes in ({'environment': 'live'}, {'capabilities': ['order.submit', 'inventory.update']},
                        {'connection_id': str(uuid.uuid4())}):
            with self.assertRaises(ValueError):
                validate_manifest(dict(manifest, **changes), self.account, 'pos')

    def test_malformed_requests_do_not_stop_service(self):
        for path, body in [('/admin/faults', {'payment': {'fail_next': -1}}),
                           (self.provider.path, {'type': 'payment.create', 'schema_version': 1}),
                           (self.provider.path, []),
                           ('/v1/accounts/not-a-uuid/payments', {})]:
            with self.assertRaises(HTTPError) as caught:
                self.client.request('POST', path, body)
            self.assertEqual(caught.exception.code, 400)
        self.assertEqual(self.client.request('GET', '/health')['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
