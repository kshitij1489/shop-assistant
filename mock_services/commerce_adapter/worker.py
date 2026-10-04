"""Durable receipt -> provider I/O -> atomic observation/outbox -> acknowledgement.

Run one worker per adapter database. Provider I/O is never in its SQLite transaction.
"""
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from urllib.error import HTTPError, URLError

from .provider import webhook_signature
from .storage import encode


log = logging.getLogger(__name__)
TRANSPORT_ERRORS = (OSError, URLError)
SUPPORTED = {'payment.create', 'payment.reconcile', 'order.submit', 'order.reconcile'}


def backoff(attempt):
    return min(3600, 2 ** min(attempt, 10) * 5)


class Worker:
    def __init__(self, store, provider, client, webhook_secret, *, clock=time.time):
        if provider.account != store.connection_id:
            raise ValueError('Fake provider account must match the adapter connection.')
        self.store, self.db, self.provider = store, store.db, provider
        self.client, self.webhook_secret, self.clock = client, webhook_secret, clock

    def original(self, command):
        data = command['data']
        if command['type'] == 'order.reconcile':
            row = self.db.execute('SELECT request FROM command_receipt WHERE command_id=?', (data['original_command_id'],)).fetchone()
            candidate = json.loads(row[0]) if row else None
            if candidate and candidate['type'] == 'order.submit' and candidate['data']['accepted_order_id'] == data['accepted_order_id']:
                return candidate
            return None
        for row in self.db.execute('SELECT request FROM command_receipt'):
            candidate = json.loads(row[0])
            if candidate['type'] == 'payment.create' and candidate['data']['payment_id'] == data['payment_id']:
                return candidate
        return None

    def validate_observation(self, observation):
        if observation['account'] != self.store.connection_id or observation['environment'] != 'test':
            raise ValueError('Provider account/environment mismatch.')
        row = self.db.execute('SELECT request FROM command_receipt WHERE provider_key=?', (observation['provider_key'],)).fetchone()
        if not row:
            raise ValueError('Observation has no original command receipt.')
        command, actual = json.loads(row[0]), observation['data']
        expected = command['data']
        if command['type'] == 'payment.create':
            if (actual['type'] != 'payment.updated' or actual['payment_id'] != expected['payment_id']
                    or actual['currency'] != expected['currency']
                    or actual['captured_minor'] not in (0, expected['amount_minor'])
                    or actual['refunded_minor'] != 0):
                raise ValueError('Provider payment does not match the accepted request.')
        elif command['type'] != 'order.submit' or actual['type'] != 'order.updated' or actual['accepted_order_id'] != expected['accepted_order_id']:
            raise ValueError('Provider order does not match the accepted request.')

    def observe(self, observation):
        """Caller owns transaction; sequence comes from authoritative fake resource."""
        self.validate_observation(observation)
        data = observation['data']
        resource, sequence = data['external_id'], data['sequence']
        previous = self.db.execute('SELECT * FROM object_state WHERE resource_id=?', (resource,)).fetchone()
        if previous and previous['provider_sequence'] >= sequence:
            if previous['provider_sequence'] == sequence and previous['observation'] != encode(observation):
                raise ValueError('Provider changed an existing resource version.')
            return
        event_id = resource + ':' + str(sequence)
        event = dict(schema_version=1, event_id=event_id, occurred_at=observation['occurred_at'], data=data)
        self.db.execute('INSERT OR REPLACE INTO object_state VALUES (?, ?, ?)', (resource, sequence, encode(observation)))
        self.db.execute('INSERT INTO event_outbox (event_id, payload) VALUES (?, ?)', (event_id, encode(event)))

    def handle(self, command, *, timeout_after_commit=False):
        # Lease metadata changes on redelivery; the immutable provider request must not.
        request = {key: value for key, value in command.items() if key not in ('lease_token', 'lease_until', 'attempt')}
        encoded = encode(request)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self.db:
            row = self.db.execute('SELECT * FROM command_receipt WHERE command_id=?', (command['command_id'],)).fetchone()
            if row and row['request_hash'] != digest:
                raise ValueError('Command ID reused with different content.')
            if not row:
                self.db.execute('INSERT INTO command_receipt (command_id, request, request_hash, provider_key, delivery) VALUES (?, ?, ?, ?, ?)',
                    (command['command_id'], encoded, digest, command['idempotency_key'], encode(command)))
            else:
                self.db.execute("UPDATE command_receipt SET delivery=?, ack_state='pending', ack_attempts=0, ack_next=0 WHERE command_id=?",
                    (encode(command), command['command_id']))
            if row and row['state'] in ('done', 'failed'):
                return
            self.db.execute('UPDATE command_receipt SET attempts=attempts+1 WHERE command_id=?', (command['command_id'],))
        # Receipt is committed BEFORE the provider can create a resource.
        observation, outcome, error = None, 'succeeded', ''
        try:
            if command['schema_version'] != 1 or command['type'] not in SUPPORTED:
                raise ValueError('Unsupported command/version.')
            if command['type'].endswith('.reconcile'):
                original = self.original(command)
                observation = self.provider.lookup(key=original['idempotency_key']) if original else None
                if observation and command['data'].get('external_id') and observation['data']['external_id'] != command['data']['external_id']:
                    raise ValueError('Reconciliation resource identity mismatch.')
            elif row and row['state'] == 'unknown':
                # An ambiguous result can only be looked up, never blindly recreated.
                observation = self.provider.lookup(key=command['idempotency_key'])
            else:
                # Crash recovery of 'processing' is safe because this provider guarantees idempotency.
                observation = self.provider.create(command, timeout_after_commit=timeout_after_commit)
            if observation is None:
                outcome, error = 'unknown', 'resource_not_found'
            else:
                self.validate_observation(observation)
        except TimeoutError:
            outcome, error = 'unknown', 'provider_timeout'
        except ValueError:
            outcome = 'failed'
            error = ('unsupported_command_version' if command['schema_version'] != 1 or command['type'] not in SUPPORTED
                     else 'invalid_provider_result_or_command')
        with self.db:
            if observation is not None and outcome == 'succeeded':
                self.observe(observation)
            self.db.execute('UPDATE command_receipt SET state=?, result=?, outcome=?, error_code=? WHERE command_id=?',
                ('done' if outcome == 'succeeded' else outcome, encode(observation) if observation else None, outcome, error, command['command_id']))

    def webhook(self, body, timestamp, signature):
        if not self.webhook_secret or len(body) > 262144:
            raise ValueError('Invalid webhook.')
        if abs(self.clock() - int(timestamp)) > 300 or not hmac.compare_digest(webhook_signature(self.webhook_secret, timestamp, body), signature):
            raise ValueError('Invalid webhook signature or timestamp.')
        notification = json.loads(body)
        if notification['account'] != self.store.connection_id or notification['environment'] != 'test':
            raise ValueError('Webhook belongs to another account/environment.')
        digest = hashlib.sha256(body).hexdigest()
        previous = self.db.execute('SELECT body_hash FROM provider_inbox WHERE event_id=?', (notification['event_id'],)).fetchone()
        if previous:
            if previous[0] != digest:
                raise ValueError('Webhook ID reused with different content.')
            return
        # Notification is only a hint: fetch authoritative state through the provider API.
        observation = self.provider.lookup(external_id=notification['resource_id'])
        if observation is None:
            raise ValueError('Unknown provider resource.')
        with self.db:
            self.observe(observation)
            self.db.execute('INSERT INTO provider_inbox VALUES (?, ?, ?, ?)',
                (notification['event_id'], digest, self.clock(), notification['resource_id']))

    def flush_acks(self):
        rows = self.db.execute("SELECT * FROM command_receipt WHERE ack_state='pending' AND outcome IS NOT NULL AND ack_next<=?", (self.clock(),)).fetchall()
        for row in rows:
            command = json.loads(row['delivery'])
            state, attempts = 'sent', row['ack_attempts'] + 1
            if datetime.fromisoformat(command['lease_until']).timestamp() <= self.clock():
                state = 'expired'  # Next lease refreshes delivery; original receipt survives.
            else:
                try:
                    self.client.acknowledge(command, row['outcome'], row['error_code'])
                except TRANSPORT_ERRORS as exc:
                    permanent = isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429
                    state = 'parked' if permanent or attempts >= 10 else 'pending'
            with self.db:
                self.db.execute('UPDATE command_receipt SET ack_state=?, ack_attempts=?, ack_next=? WHERE command_id=?',
                    (state, attempts, self.clock() + backoff(attempts), row['command_id']))

    def flush_events(self):
        for row in self.db.execute("SELECT * FROM event_outbox WHERE state='pending' AND next_attempt<=?", (self.clock(),)).fetchall():
            attempts, state, error = row['attempts'] + 1, 'sent', ''
            try:
                result = self.client.send_event(json.loads(row['payload']))
                # A 202/failed inbox receipt is not a processed business event.
                if result.get('status') != 'processed':
                    state, error = 'pending', 'engine_not_processed'
            except TRANSPORT_ERRORS as exc:
                permanent = isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429
                state = 'parked' if permanent else 'pending'
                error = 'http_' + str(exc.code) if isinstance(exc, HTTPError) else 'transport_error'
            if state == 'pending' and attempts >= 10:
                state = 'parked'
            with self.db:
                self.db.execute('UPDATE event_outbox SET state=?, attempts=?, next_attempt=?, error=? WHERE event_id=?',
                    (state, attempts, self.clock() + backoff(attempts), error, row['event_id']))

    def tick(self):
        # Resume the crash window after receipt insertion without waiting for a new lease.
        for row in self.db.execute("SELECT delivery FROM command_receipt WHERE state='processing'").fetchall():
            delivery = json.loads(row[0])
            if datetime.fromisoformat(delivery['lease_until']).timestamp() > self.clock():
                self.handle(delivery)
        self.flush_events()
        self.flush_acks()
        try:
            commands = self.client.claim()
        except TRANSPORT_ERRORS as exc:
            log.warning('Command poll failed: %s', type(exc).__name__)
            return
        for command in commands:
            self.handle(command)
        self.flush_events()
        self.flush_acks()
