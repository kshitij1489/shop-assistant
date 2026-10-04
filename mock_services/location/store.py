"""Durable per-account emulator state. Every HTTP request owns its connection."""
import json
import re
from datetime import datetime, timezone

from ..commerce_adapter.storage import connect, encode
from .fixtures import validate_fixture

ACCOUNT = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$')
SERVICES = ('geocoding', 'reverse_geocoding')
OUTCOMES = ('success', 'unavailable', 'timeout', 'postal_mismatch')
LIMITS = dict(delay_ms=30000, timeout_hold_ms=30000, next=1000)
REQUEST_LOG_LIMIT = 200


def validate_account(account):
    if not isinstance(account, str) or not ACCOUNT.fullmatch(account):
        raise ValueError('Location account must be a tenant slug-like identifier.')
    return account


def validate_outcome(entry):
    if not isinstance(entry, dict) or set(entry) - {'outcome', 'postal_code'}:
        raise ValueError('Outcome entries contain outcome and optional postal_code.')
    if entry.get('outcome') not in OUTCOMES:
        raise ValueError('Unknown lookup outcome.')
    postal = entry.get('postal_code')
    if entry['outcome'] == 'postal_mismatch':
        if not isinstance(postal, str) or not re.fullmatch(r'[1-9][0-9]{5}', postal):
            raise ValueError('postal_mismatch requires a six-digit postal_code.')
        return dict(outcome='postal_mismatch', postal_code=postal)
    if postal is not None:
        raise ValueError('postal_code is only valid for postal_mismatch.')
    return dict(outcome=entry['outcome'])


def validate_controls(payload):
    """Per-service control: sticky default outcome, FIFO queue and delays."""
    if not isinstance(payload, dict) or set(payload) - set(SERVICES):
        raise ValueError('Expected geocoding or reverse_geocoding controls.')
    result = {}
    for service, rule in payload.items():
        if not isinstance(rule, dict) or set(rule) - {'default', 'next', 'delay_ms', 'timeout_hold_ms'}:
            raise ValueError('Unknown location control field.')
        default = rule.get('default', 'success')
        default = validate_outcome(default if isinstance(default, dict) else {'outcome': default})
        queue = rule.get('next', [])
        if not isinstance(queue, list) or len(queue) > LIMITS['next']:
            raise ValueError('next must be a bounded list of outcomes.')
        for key in ('delay_ms', 'timeout_hold_ms'):
            value = rule.get(key, 0)
            if type(value) is not int or not 0 <= value <= LIMITS[key]:
                raise ValueError(key + ' must be a bounded nonnegative integer.')
        result[service] = dict(default=default, next=[validate_outcome(item) for item in queue],
                               delay_ms=rule.get('delay_ms', 0), timeout_hold_ms=rule.get('timeout_hold_ms', 10000))
    return result


class LocationStore:
    def __init__(self, path, account):
        self.db = connect(path)
        self.account = validate_account(account)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS location_fixtures (
                account TEXT NOT NULL, fixture_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (account, fixture_id));
            CREATE TABLE IF NOT EXISTS location_controls (
                account TEXT NOT NULL, service TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (account, service));
            CREATE TABLE IF NOT EXISTS location_requests (
                account TEXT NOT NULL, sequence INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (account, sequence));
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def _transaction(self, work):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = work()
            self.db.commit()
            return result
        except BaseException:
            self.db.rollback()
            raise

    def replace_fixtures(self, fixtures):
        """Replace this account's fixtures atomically; identical input is a no-op."""
        if not isinstance(fixtures, list) or len(fixtures) > 500:
            raise ValueError('fixtures must be a list of at most 500 entries.')
        canonical = [validate_fixture(fixture) for fixture in fixtures]
        if len({fixture['fixture_id'] for fixture in canonical}) != len(canonical):
            raise ValueError('Duplicate fixture_id.')

        def work():
            self.db.execute('DELETE FROM location_fixtures WHERE account=?', (self.account,))
            self.db.executemany('INSERT INTO location_fixtures VALUES (?, ?, ?)',
                                [(self.account, fixture['fixture_id'], encode(fixture)) for fixture in canonical])
            return canonical
        return self._transaction(work)

    def fixtures(self):
        return [json.loads(row[0]) for row in self.db.execute(
            'SELECT payload FROM location_fixtures WHERE account=? ORDER BY fixture_id', (self.account,))]

    def replace_controls(self, payload):
        """Upsert services present in the payload; leave other services' controls in place.

        An empty object clears every service for the account (explicit reset).
        """
        controls = validate_controls(payload)

        def work():
            if not controls:
                self.db.execute('DELETE FROM location_controls WHERE account=?', (self.account,))
                return {}
            for service, rule in controls.items():
                self._save_control(service, rule)
            return {row['service']: json.loads(row['payload']) for row in self.db.execute(
                'SELECT service, payload FROM location_controls WHERE account=?', (self.account,))}
        return self._transaction(work)

    def queue_outcome(self, service, entry):
        """Append one outcome for the next request of this service."""
        if service not in SERVICES:
            raise ValueError('Unknown location service.')
        entry = validate_outcome(entry)

        def work():
            rule = self._control(service)
            if len(rule['next']) >= LIMITS['next']:
                raise ValueError('Outcome queue is full.')
            rule['next'].append(entry)
            self._save_control(service, rule)
            return rule
        return self._transaction(work)

    def _control(self, service):
        row = self.db.execute('SELECT payload FROM location_controls WHERE account=? AND service=?',
                              (self.account, service)).fetchone()
        return json.loads(row[0]) if row else validate_controls({service: {}})[service]

    def _save_control(self, service, rule):
        self.db.execute('INSERT OR REPLACE INTO location_controls VALUES (?, ?, ?)',
                        (self.account, service, encode(rule)))

    def controls(self):
        return {row['service']: json.loads(row['payload']) for row in self.db.execute(
            'SELECT service, payload FROM location_controls WHERE account=?', (self.account,))}

    def take_outcome(self, service):
        """Consume the next queued outcome (or the sticky default). Caller owns the transaction."""
        rule = self._control(service)
        entry = rule['next'].pop(0) if rule['next'] else dict(rule['default'])
        self._save_control(service, rule)
        return entry, rule['delay_ms'], rule['timeout_hold_ms']

    def record(self, entry):
        """Append a redacted request record. Caller owns the transaction."""
        row = self.db.execute('SELECT COALESCE(MAX(sequence), 0) FROM location_requests WHERE account=?',
                              (self.account,)).fetchone()
        sequence = row[0] + 1
        payload = dict(entry, sequence=sequence, at=datetime.now(timezone.utc).isoformat())
        self.db.execute('INSERT INTO location_requests VALUES (?, ?, ?)', (self.account, sequence, encode(payload)))
        return payload

    def requests(self, limit=REQUEST_LOG_LIMIT):
        rows = self.db.execute('SELECT payload FROM location_requests WHERE account=? ORDER BY sequence DESC LIMIT ?',
                               (self.account, limit)).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def reset(self):
        def work():
            for table in ('location_fixtures', 'location_controls', 'location_requests'):
                self.db.execute(f'DELETE FROM {table} WHERE account=?', (self.account,))
        self._transaction(work)
