"""Configuration and startup contracts, independent of live services."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


from tests.support.paths import REPOSITORY_ROOT as ROOT


class PublicURLSettingsTests(unittest.TestCase):
    def settings_for(self, origin):
        return subprocess.run([sys.executable, '-c',
            'import json; from studio_desk import settings as s; '
            'print(json.dumps([s.PUBLIC_URL, s.ALLOWED_HOSTS, s.CSRF_TRUSTED_ORIGINS, '
            's.SECURE_SSL_REDIRECT, s.SESSION_COOKIE_SECURE, s.CSRF_COOKIE_SECURE, s.SECURE_HSTS_SECONDS]))'],
            cwd=ROOT, capture_output=True, text=True, env={
                'PATH': os.environ['PATH'], 'STUDIO_ENV_FILE': '/dev/null',
                'SECRET_KEY': 'settings-test-only', 'JWT_SECRET': 'settings-test-only', 'PUBLIC_URL': origin,
            })

    def test_local_http_needs_no_provider_credentials_or_deployment_domain(self):
        result = self.settings_for('http://localhost:8080/')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            'http://localhost:8080', ['localhost'], ['http://localhost:8080'], False, False, False, 0])

    def test_https_uses_configured_host_and_secure_cookies(self):
        result = self.settings_for('https://my-cafe.example.org')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            'https://my-cafe.example.org', ['my-cafe.example.org'], ['https://my-cafe.example.org'],
            True, True, True, 31536000])

    def test_rejects_non_origin_public_urls(self):
        for origin in ('ftp://example.org', 'https://example.org/app', 'https://user:pass@example.org',
                       'https://example.org/?query=yes', 'https://example.org/#fragment'):
            with self.subTest(origin=origin):
                self.assertNotEqual(self.settings_for(origin).returncode, 0)


class EntrypointFailureTests(unittest.TestCase):
    def test_failed_migration_or_static_collection_prevents_application_start(self):
        for failing_command in ('migrate', 'collectstatic'):
            with self.subTest(command=failing_command), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                log = root / 'calls'
                stubs = {
                    'id': '#!/bin/sh\necho 1000\n',
                    'nc': '#!/bin/sh\nexit 0\n',
                    'mkdir': '#!/bin/sh\nexit 0\n',
                    'python': '#!/bin/sh\necho "$*" >> "$CALL_LOG"\nif [ "$2" = "$FAIL_COMMAND" ]; then exit 7; fi\n',
                    'start-application': '#!/bin/sh\necho started >> "$CALL_LOG"\n',
                }
                for name, content in stubs.items():
                    path = root / name
                    path.write_text(content)
                    path.chmod(0o755)
                result = subprocess.run(['bash', str(ROOT / 'entrypoint.sh'), 'start-application'],
                    cwd=root, capture_output=True, text=True, env={**os.environ,
                        'PATH': f'{root}:{os.environ["PATH"]}', 'CALL_LOG': str(log),
                        'FAIL_COMMAND': failing_command, 'RUN_ENTRYPOINT_ACTIONS': 'True',
                        'RUN_COLLECTSTATIC': 'True', 'WAIT_FOR_REDIS': 'false',
                    })
                self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
                self.assertNotIn('started', log.read_text())
