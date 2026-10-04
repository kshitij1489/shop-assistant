"""Serialize browser conversations without a transaction around provider calls."""
from contextlib import contextmanager
from hashlib import sha256
from threading import RLock
from time import monotonic, sleep

from django.db import connection

_sqlite_locks = {}


@contextmanager
def browser_turn_lock(session_key):
    # Lock the browser, not the tenant namespace: Django persists the entire
    # browser session. A stable digest also works across processes and hosts.
    key = int.from_bytes(sha256(('cafe-browser:' + session_key).encode()).digest()[:8],
                         byteorder='big', signed=True)
    if connection.vendor == 'sqlite':
        # SQLite is used by the offline test/development configuration only.
        with _sqlite_locks.setdefault(key, RLock()):
            yield
        return
    if connection.vendor != 'postgresql':
        raise RuntimeError('Browser conversation locking requires PostgreSQL')
    deadline = monotonic() + 60
    with connection.cursor() as cursor:
        while True:
            cursor.execute('SELECT pg_try_advisory_lock(%s)', [key])
            if cursor.fetchone()[0]:
                break
            if monotonic() >= deadline:
                raise RuntimeError('Could not acquire browser conversation lock')
            sleep(.05)
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s)', [key])
