"""Verify shared development resources and the website/worker actually serving a run."""

import json
import os
from pathlib import Path
import ssl
from time import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import uuid4


REPOSITORY = Path(__file__).resolve().parents[2]
LOOPBACK = frozenset({'127.0.0.1', 'localhost', '::1'})


def _inside_repository(path):
    return path.resolve().is_relative_to(REPOSITORY)


def _https_local_adapter(url):
    parsed = urlsplit(url)
    return (parsed.scheme == 'https' and parsed.hostname in LOOPBACK | {'adapter_https'} and not parsed.username
            and not parsed.password and not parsed.query and not parsed.fragment
            and parsed.path in ('', '/'))


def run_preflight(*, base_url=None, evidence_directory=None, skip_location=None):
    from django.conf import settings
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.core import signing
    from django.core.cache import cache
    from redis import Redis
    from .health import PATH, SALT, runtime_configuration

    checks, blockers = {}, []

    def check(name, fn):
        try:
            result = fn()
            row = result if isinstance(result, dict) else {'ok': bool(result)}
        except Exception as exc:
            row = {'ok': False, 'error_type': type(exc).__name__}
        checks[name] = row
        if not row.get('ok'):
            blockers.append({'code': name, 'message': f'Evaluation preflight failed: {name}.'})
        return row.get('ok', False)

    check('evaluation_enabled', lambda: getattr(settings, 'EVALUATION_ENABLED', False) is True)
    parsed_base = urlsplit(base_url or '')
    check('base_url_loopback', lambda: parsed_base.hostname in LOOPBACK and
          parsed_base.scheme in {'http', 'https'} and not parsed_base.username and not parsed_base.password)
    root = Path(getattr(settings, 'EVALUATION_EVIDENCE_ROOT', '') or '/var/tmp/studio-eval').resolve()
    evidence = Path(evidence_directory or root).resolve()
    check('evidence_outside_repo', lambda: not _inside_repository(evidence) and
          (evidence == root or evidence.parent == root))
    check('development_environment', lambda: settings.DEBUG is True)
    db = settings.DATABASES['default']
    check('database_postgresql', lambda: db.get('ENGINE') == 'django.db.backends.postgresql')
    check('cache_redis', lambda: urlsplit(settings.APP_REDIS_URL).scheme in {'redis', 'rediss'} and
          urlsplit(settings.APP_SESSION_REDIS_URL).scheme in {'redis', 'rediss'})

    def migrations():
        connection.ensure_connection()
        executor = MigrationExecutor(connection)
        return not executor.migration_plan(executor.loader.graph.leaf_nodes())

    if checks['database_postgresql']['ok']:
        check('database_migrations', migrations)
    else:
        check('database_migrations', lambda: {'ok': False, 'reason': 'PostgreSQL configuration is unverified'})
    adapter = os.environ.get('EVALUATION_ADAPTER_URL', '')

    def adapter_health():
        if not _https_local_adapter(adapter):
            return False
        context = ssl.create_default_context(cafile=os.environ.get('EVALUATION_ADAPTER_CA'))
        with urlopen(adapter.rstrip('/') + '/health', timeout=3, context=context) as response:
            return json.load(response).get('service') == 'evaluation-adapter'

    check('adapter_https', adapter_health)
    if blockers:
        return {'schema_version': '1.0.0', 'valid': False, 'blockers': blockers, 'checks': checks}

    nonce = uuid4().hex
    probe_file = root / ('.health-' + nonce)
    probe_session = None
    session_cache = Redis.from_url(settings.APP_SESSION_REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe_file.write_text(nonce)
        # The same nonce must be visible through the DB, both caches and evidence mount.
        from django.contrib.sessions.models import Session
        from django.utils import timezone
        from datetime import timedelta
        Session.objects.create(session_key=nonce, session_data='', expire_date=timezone.now() + timedelta(minutes=1))
        probe_session = nonce
        cache.set('eval-health:' + nonce, nonce, timeout=60)
        session_cache.set('eval-health:' + nonce, nonce, ex=60)
        expected = runtime_configuration()

        def website():
            header = signing.dumps({'nonce': nonce}, salt=SALT)
            request = Request(base_url.rstrip('/') + PATH, headers={'X-Evaluation-Health': header})
            with urlopen(request, timeout=5) as response:
                actual = json.load(response)
            return {'ok': actual == {**expected, 'shared_resources': True}, 'configuration': actual}

        check('website', website)

        def worker():
            from .tasks import health
            pending = health.apply_async(args=[nonce], queue='default')
            try:
                actual = pending.get(timeout=8)
            finally:
                pending.forget()
            return {'ok': actual == {**expected, 'shared_resources': True}, 'configuration': actual}

        check('worker', worker)
        check('beat', lambda: 0 <= time() - float(cache.get('evaluate:beat:heartbeat') or 0) < 45)
    except Exception as exc:
        check('shared_resources', lambda: {'ok': False, 'error_type': type(exc).__name__})
    finally:
        check('probe_file_cleanup', lambda: (probe_file.unlink(missing_ok=True), not probe_file.exists())[1])
        if probe_session:
            probe_rows = Session.objects.filter(session_key=probe_session)
            check('probe_database_cleanup', lambda: (probe_rows.delete(), not probe_rows.exists())[1])
        check('probe_cache_cleanup', lambda: (cache.delete('eval-health:' + nonce),
                                             cache.get('eval-health:' + nonce) is None)[1])
        check('probe_session_cleanup', lambda: (session_cache.delete('eval-health:' + nonce),
                                               not session_cache.exists('eval-health:' + nonce))[1])
        session_cache.close()
    return {'schema_version': '1.0.0', 'valid': not blockers, 'blockers': blockers, 'checks': checks}
