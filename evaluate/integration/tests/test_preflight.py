"""Preflight must verify probe removal, including backends that silently do nothing."""
from contextlib import ExitStack
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from evaluate.integration.preflight import run_preflight


class PreflightCleanupTests(SimpleTestCase):
    def run_probe(self, stuck=None):
        with TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(override_settings(
                DEBUG=True, EVALUATION_ENABLED=True, EVALUATION_EVIDENCE_ROOT=directory,
                EVALUATION_ORIGINAL_DATABASE={'NAME': 'development'},
                EVALUATION_ORIGINAL_REDIS_URL='redis://development/2',
                APP_REDIS_URL='redis://development/2', APP_SESSION_REDIS_URL='redis://development/1',
                LOCATION_PROVIDER='emulator', LOCATION_EMULATOR_URL='http://localhost:8080',
            ))
            stack.enter_context(patch.dict('django.conf.settings.DATABASES', {
                'default': {'ENGINE': 'django.db.backends.postgresql', 'NAME': 'development'},
            }))
            stack.enter_context(patch.dict('os.environ', {
                'EVALUATION_DATABASE_URL': '',
                'EVALUATION_REDIS_URL': '',
                'EVALUATION_ADAPTER_URL': 'https://localhost:8443',
            }))
            stack.enter_context(patch('django.db.connection'))
            executor = stack.enter_context(patch('django.db.migrations.executor.MigrationExecutor'))
            executor.return_value.migration_plan.return_value = []
            stack.enter_context(patch('evaluate.integration.preflight.ssl.create_default_context'))
            stack.enter_context(patch('evaluate.integration.preflight.urlopen', side_effect=[
                io.StringIO(json.dumps({'service': 'evaluation-adapter'})),
                io.StringIO(json.dumps({'shared_resources': True})),
            ]))
            stack.enter_context(patch('evaluate.integration.health.runtime_configuration', return_value={}))
            health, redis = Mock(), Mock()
            redis_factory = Mock()
            redis_factory.from_url.return_value = redis
            stack.enter_context(patch.dict('sys.modules', {
                'evaluate.integration.tasks': SimpleNamespace(health=health),
                'redis': SimpleNamespace(Redis=redis_factory),
            }))
            health.apply_async.return_value.get.return_value = {'shared_resources': True}
            sessions = stack.enter_context(patch('django.contrib.sessions.models.Session.objects'))
            rows = sessions.filter.return_value
            rows.delete.return_value = (0, {}) if stuck == 'database' else (1, {})
            rows.exists.return_value = stuck == 'database'
            cache = stack.enter_context(patch('django.core.cache.cache'))
            cache.delete.return_value = stuck != 'cache'
            cache.get.side_effect = lambda key: time() - 1 if key == 'evaluate:beat:heartbeat' else (
                'leftover' if stuck == 'cache' else None)
            redis.delete.return_value = 0 if stuck == 'session' else 1
            redis.exists.return_value = int(stuck == 'session')
            if stuck == 'file':
                stack.enter_context(patch.object(Path, 'unlink'))
            elif stuck == 'file_error':
                stack.enter_context(patch.object(Path, 'unlink', side_effect=PermissionError))
            report = run_preflight(base_url='http://localhost:8000', skip_location=True)
            # A file cleanup failure must not prevent the other cleanup attempts.
            rows.delete.assert_called_once()
            cache.delete.assert_called_once()
            redis.delete.assert_called_once()
            redis.close.assert_called_once()
            return report

    def test_shared_development_resources_and_removed_probes_pass(self):
        report = self.run_probe()
        self.assertTrue(report['valid'], report['blockers'])
        for resource in ('database', 'cache', 'session', 'file'):
            self.assertTrue(report['checks'][f'probe_{resource}_cleanup']['ok'])

    def test_leftover_probes_fail_even_when_delete_returns_normally(self):
        for resource in ('database', 'cache', 'session', 'file'):
            with self.subTest(resource=resource):
                report = self.run_probe(stuck=resource)
                self.assertFalse(report['valid'])
                self.assertEqual([b['code'] for b in report['blockers']], [f'probe_{resource}_cleanup'])

    def test_file_cleanup_exception_is_reported_and_other_probes_are_cleaned(self):
        report = self.run_probe(stuck='file_error')
        self.assertFalse(report['valid'])
        self.assertEqual(report['checks']['probe_file_cleanup'], {'ok': False, 'error_type': 'PermissionError'})
