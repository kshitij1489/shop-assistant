"""Offline wire-level walkthrough; real engine coverage lives in tests/integration/test_reference_adapter.py."""
import tempfile
import uuid
from pathlib import Path

from .fixtures import command, identifier, order_command, payment_command
from .provider import FakeProvider
from .storage import Store, encode
from .worker import Worker


class DemoClient:
    """A deliberately small transport stand-in, not Studio Desk checkout logic."""
    def __init__(self):
        self.commands, self.events, self.acks = [], {}, []
        self.lose_event_response = False

    def claim(self):
        result, self.commands = self.commands, []
        return result

    def acknowledge(self, command, outcome, error_code=''):
        self.acks.append((command['command_id'], outcome))
        return {'ok': True}

    def send_event(self, event):
        previous = self.events.setdefault(event['event_id'], event)
        if previous != event:
            raise ValueError('Event identity changed.')
        if self.lose_event_response:
            self.lose_event_response = False
            raise TimeoutError('Event accepted but response lost.')
        return {'event_id': event['event_id'], 'status': 'processed'}


def run_demo(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='walkthrough-', dir=directory))
    for scenario in ('happy', 'duplicate', 'timeout'):
        path = root / scenario
        path.mkdir()
        workers = {}
        for role in ('payment', 'pos'):
            account = identifier(role + '-connection')
            store = Store(path / (role + '.sqlite3'), account)
            provider = FakeProvider(path / 'provider.sqlite3', account)
            workers[role] = Worker(store, provider, DemoClient(), 'demo-webhook-secret')
        payment, pos = workers['payment'], workers['pos']
        create = payment_command()
        print(f'[{scenario}] Confirmation fixture: EUR 12.50, awaiting payment')
        payment.handle(create, timeout_after_commit=scenario == 'timeout')
        payment.flush_acks()
        if scenario == 'timeout':
            assert payment.client.acks[-1][1] == 'unknown'
            # Close/reopen the adapter DB to prove recovery uses durable receipts.
            payment.store.close()
            payment = Worker(Store(path / 'payment.sqlite3', identifier('payment-connection')),
                payment.provider, payment.client, 'demo-webhook-secret')
            workers['payment'] = payment
            payment.handle(command('payment.reconcile', {'payment_id': create['data']['payment_id'], 'external_id': ''}))
            print('[timeout] Restarted; looked up the original payment key after a lost provider response')
        payment.flush_events()
        body, stamp, signature = payment.provider.signed_capture(create['data']['payment_id'], payment.webhook_secret)
        payment.webhook(body, stamp, signature)
        if scenario == 'duplicate':
            payment.webhook(body, stamp, signature)
            payment.handle({**create, 'lease_token': str(uuid.uuid4()), 'attempt': 2})
            payment.client.lose_event_response = True
        payment.flush_events()
        if scenario == 'duplicate':
            with payment.db:
                payment.db.execute('UPDATE event_outbox SET next_attempt=0')
            payment.flush_events()
        captured = next(event['data'] for event in payment.client.events.values() if event['data']['status'] == 'captured')
        print(f'[{scenario}] Verified capture delivered; submitting the accepted snapshot to fake POS')
        submit = order_command(captured)
        pos.handle(submit, timeout_after_commit=scenario == 'timeout')
        if scenario == 'timeout':
            pos.handle(command('order.reconcile', dict(accepted_order_id=submit['data']['accepted_order_id'],
                order_id=submit['data']['order_id'], original_command_id=submit['command_id'])))
        if scenario == 'duplicate':
            pos.handle({**submit, 'lease_token': str(uuid.uuid4()), 'attempt': 2})
        pos.flush_events()
        payment.flush_acks()
        pos.flush_acks()
        assert len(payment.client.events) == 2 and len(pos.client.events) == 1
        for worker in workers.values():
            count = worker.provider.db.execute('SELECT COUNT(*) FROM resources WHERE account=?', (worker.store.connection_id,)).fetchone()[0]
            assert count == 1, 'Duplicate provider side effect'
            assert all(row['state'] == 'sent' for row in worker.store.summary()['outbox'])
        print(f'[{scenario}] PASS: one payment, one POS order, all events processed')
        (path / 'summary.json').write_text(encode({role: worker.store.summary() for role, worker in workers.items()}) + '\n')
        for worker in workers.values():
            worker.store.close()
            worker.provider.close()
    print('SQLite databases and summaries:', root)
    return root
