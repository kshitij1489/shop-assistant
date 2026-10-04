"""Process lock shared by the reference adapter and mock service commands."""
import fcntl
from contextlib import contextmanager


@contextmanager
def worker_lock(path):
    # OS releases the lock even after a crash. All entry points touching adapter state use it.
    with open(str(path) + '.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another worker owns this adapter database.')
        yield
