"""Fake provider API with its own durable database and idempotency ledger.

Only this module accesses provider tables. Adapter code talks through these methods,
as it would through an authenticated provider SDK. No real money is moved.
"""
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

from .storage import connect, encode


def webhook_signature(secret, timestamp, body):
    return hmac.new(secret.encode(), str(timestamp).encode() + b'.' + body, hashlib.sha256).hexdigest()


class FakeProvider:
    def __init__(self, path, account):
        self.db = connect(path)
        self.account = str(account)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS resources (
                account TEXT NOT NULL, resource_id TEXT NOT NULL,
                provider_key TEXT NOT NULL, request TEXT NOT NULL, observation TEXT NOT NULL,
                PRIMARY KEY (account, resource_id), UNIQUE (account, provider_key)
            );
        ''')

    def create(self, command, *, timeout_after_commit=False):
        key, data = command['idempotency_key'], command['data']
        request = encode({'type': command['type'], 'data': data})
        # Serialize idempotency check and side effect, including across processes.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT * FROM resources WHERE account=? AND provider_key=?', (self.account, key)).fetchone()
            if row:
                if row['request'] != request:
                    raise ValueError('Provider idempotency key reused with different input.')
                self.db.commit()
                return json.loads(row['observation'])
            resource_id = 'fake-' + str(uuid.uuid4())
            if command['type'] == 'payment.create':
                observation = dict(type='payment.updated', payment_id=data['payment_id'],
                    currency=data['currency'], status='pending', captured_minor=0,
                    refunded_minor=0, checkout_url='https://payments.example.test/' + resource_id)
            elif command['type'] == 'order.submit':
                snapshot_hash = hashlib.sha256(encode(data['snapshot']).encode()).hexdigest()
                if snapshot_hash != data['snapshot_hash']:
                    raise ValueError('Accepted snapshot hash does not match.')
                observation = dict(type='order.updated', accepted_order_id=data['accepted_order_id'], status='accepted')
            else:
                raise ValueError('Fake provider supports payment.create and order.submit only.')
            result = dict(account=self.account, environment='test', provider_key=key,
                occurred_at=datetime.now(timezone.utc).isoformat(),
                data={**observation, 'external_id': resource_id, 'sequence': 1})
            self.db.execute('INSERT INTO resources VALUES (?, ?, ?, ?, ?)',
                (self.account, resource_id, key, request, encode(result)))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        if timeout_after_commit:
            raise TimeoutError('Provider committed but the response was lost.')
        return result

    def lookup(self, *, key=None, external_id=None):
        column, value = ('provider_key', key) if key else ('resource_id', external_id)
        row = self.db.execute(f'SELECT observation FROM resources WHERE account=? AND {column}=?', (self.account, value)).fetchone()
        return json.loads(row[0]) if row else None

    def capture(self, payment_id):
        """Fake customer action; returns a signed-webhook-ready notification."""
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute('SELECT * FROM resources WHERE account=?', (self.account,)).fetchall()
            row = next((row for row in rows if json.loads(row['request'])['type'] == 'payment.create'
                        and json.loads(row['request'])['data']['payment_id'] == payment_id), None)
            if row is None:
                raise ValueError('Unknown fake payment.')
            result = json.loads(row['observation'])
            if result['data']['status'] == 'pending':
                result['data'].update(status='captured', captured_minor=json.loads(row['request'])['data']['amount_minor'],
                                      sequence=result['data']['sequence'] + 1, checkout_url='')
                result['occurred_at'] = datetime.now(timezone.utc).isoformat()
                self.db.execute('UPDATE resources SET observation=? WHERE account=? AND resource_id=?',
                    (encode(result), self.account, row['resource_id']))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {'event_id': row['resource_id'] + ':' + str(result['data']['sequence']),
                'account': self.account, 'environment': 'test', 'resource_id': row['resource_id']}

    def signed_capture(self, payment_id, secret):
        body = encode(self.capture(payment_id)).encode()
        stamp = str(int(time.time()))
        return body, stamp, webhook_signature(secret, stamp, body)

    def close(self):
        self.db.close()
