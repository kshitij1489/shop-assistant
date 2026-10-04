import json
import sqlite3


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    return db


class Store:
    def __init__(self, path, connection_id):
        self.db = connect(path)
        self.connection_id = str(connection_id)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (connection_id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS command_receipt (
                command_id TEXT PRIMARY KEY, request TEXT NOT NULL,
                request_hash TEXT NOT NULL, provider_key TEXT NOT NULL UNIQUE,
                delivery TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'processing',
                attempts INTEGER NOT NULL DEFAULT 0, result TEXT,
                outcome TEXT, error_code TEXT NOT NULL DEFAULT '',
                ack_state TEXT NOT NULL DEFAULT 'pending',
                ack_attempts INTEGER NOT NULL DEFAULT 0, ack_next REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS provider_inbox (
                event_id TEXT PRIMARY KEY, body_hash TEXT NOT NULL,
                verified_at REAL NOT NULL, resource_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS object_state (
                resource_id TEXT PRIMARY KEY, provider_sequence INTEGER NOT NULL,
                observation TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS event_outbox (
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
            );
        ''')
        with self.db:
            rows = self.db.execute('SELECT connection_id FROM metadata').fetchall()
            if rows and rows[0][0] != self.connection_id:
                raise ValueError('Adapter database belongs to another connection.')
            self.db.execute('INSERT OR IGNORE INTO metadata VALUES (?)', (self.connection_id,))

    def summary(self):
        return {table: [dict(row) for row in self.db.execute(query)] for table, query in {
            'receipts': 'SELECT command_id, state, attempts, outcome, error_code, ack_state FROM command_receipt',
            'outbox': 'SELECT event_id, state, attempts, error FROM event_outbox',
            'webhooks': 'SELECT event_id, resource_id FROM provider_inbox',
        }.items()}

    def close(self):
        self.db.close()
