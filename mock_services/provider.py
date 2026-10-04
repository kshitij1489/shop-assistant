"""Persistent mock state. Every HTTP request owns its SQLite connection."""
import json
from datetime import datetime, timezone
from uuid import UUID

from .commerce_adapter.provider import FakeProvider
from .commerce_adapter.storage import encode


class Provider(FakeProvider):
    def __init__(self, path, account):
        super().__init__(path, str(UUID(account)))
        self.db.execute('''CREATE TABLE IF NOT EXISTS menu_exports (
            account TEXT NOT NULL, generation TEXT NOT NULL, sequence INTEGER NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY (account, generation))''')
        self.db.commit()

    def resources(self, kind):
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT observation FROM resources WHERE account=? ORDER BY resource_id', (self.account,))
            if json.loads(row[0])['data']['type'] == kind + '.updated']

    def resource_summary(self):
        """Redacted per-account payment/order states for evaluation evidence."""
        summary = dict(payments=[], orders=[])
        for observation in self.resources('payment'):
            data = observation['data']
            summary['payments'].append(dict(payment_id=data['payment_id'], external_id=data['external_id'],
                status=data['status'], captured_minor=data['captured_minor'], sequence=data['sequence']))
        for observation in self.resources('order'):
            data = observation['data']
            summary['orders'].append(dict(accepted_order_id=data['accepted_order_id'], external_id=data['external_id'],
                status=data['status'], sequence=data['sequence']))
        return summary

    @staticmethod
    def _check_expected(expected, request_data):
        """Scenario controls state the expected capture; a mismatch must not capture."""
        if not isinstance(expected, dict) or set(expected) - {'amount_minor', 'currency'}:
            raise ValueError('Payment action body accepts amount_minor and currency only.')
        if 'amount_minor' in expected and expected['amount_minor'] != request_data['amount_minor']:
            raise ValueError('Expected amount does not match the payment.')
        if 'currency' in expected and expected['currency'] != request_data['currency']:
            raise ValueError('Expected currency does not match the payment.')

    def transition_payment(self, payment_id, status, *, expected=None):
        if status not in ('captured', 'failed', 'cancelled'):
            raise ValueError('Unsupported payment status.')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            rows = self.db.execute('SELECT * FROM resources WHERE account=?', (self.account,)).fetchall()
            row = next((r for r in rows if json.loads(r['request'])['type'] == 'payment.create'
                        and json.loads(r['request'])['data']['payment_id'] == payment_id), None)
            if row is None:
                raise LookupError('Unknown payment.')
            self._check_expected({} if expected is None else expected, json.loads(row['request'])['data'])
            observation = json.loads(row['observation'])
            data = observation['data']
            if data['status'] != status:
                if data['status'] != 'pending':
                    raise ValueError('Payment already has a different terminal status.')
                data.update(status=status, sequence=data['sequence'] + 1, checkout_url='')
                if status == 'captured':
                    data['captured_minor'] = json.loads(row['request'])['data']['amount_minor']
                observation['occurred_at'] = datetime.now(timezone.utc).isoformat()
                self.db.execute('UPDATE resources SET observation=? WHERE account=? AND resource_id=?',
                                (encode(observation), self.account, row['resource_id']))
            self.db.commit()
            return observation
        except BaseException:
            self.db.rollback()
            raise

    def export_menu(self, catalog, generation, after_sequence=0):
        generation = str(UUID(generation))
        if type(after_sequence) is not int or not 0 <= after_sequence < 9223372036854775807:
            raise ValueError('after_sequence must be a nonnegative integer below the sequence limit.')
        self.db.execute('BEGIN IMMEDIATE')
        try:
            row = self.db.execute('SELECT sequence FROM menu_exports WHERE account=? AND generation=?',
                                  (self.account, generation)).fetchone()
            sequence = max(row[0] if row else 0, after_sequence) + 1
            if sequence > 9223372036854775807:
                raise ValueError('Menu sequence exhausted.')
            snapshot = dict(catalog, schema_version=1, complete=True, source_generation=generation,
                sequence=sequence, revision='mock-menu-v1', observed_at=datetime.now(timezone.utc).isoformat())
            self.db.execute('INSERT OR REPLACE INTO menu_exports VALUES (?, ?, ?, ?)',
                            (self.account, generation, sequence, encode(snapshot)))
            self.db.commit()
            return snapshot
        except BaseException:
            self.db.rollback()
            raise
