"""Development evaluation must preserve the application's existing resources."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class EvaluationSettingsTests(unittest.TestCase):
    def settings_process(self, code, **overrides):
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('EVALUATION_')}
        environment.update({
            'STUDIO_ENV_FILE': '/dev/null', 'DEBUG': 'true',
            'SECRET_KEY': 'development-test-secret', 'JWT_SECRET': 'development-test-jwt',
            'DJANGO_SETTINGS_MODULE': 'evaluate.integration.settings',
            'POSTGRES_DB': 'existing_development',
            'CELERY_BROKER_URL': 'redis://development:6379/4',
            'APP_REDIS_URL': 'redis://development:6379/5',
            'APP_SESSION_REDIS_URL': 'redis://development:6379/6',
            'LLM_MODEL': 'development-chat-model',
            'LLM_TRANSLATE_MODEL': 'development-translate-model',
            'LLM_ANALYTICS_MODEL': 'development-analytics-model',
            'LOCATION_PROVIDER': 'google', **overrides,
        })
        return subprocess.run([sys.executable, '-c', code],
                              cwd=Path(__file__).resolve().parents[3], env=environment,
                              capture_output=True, text=True)

    def test_uses_development_database_caches_models_and_keys(self):
        result = self.settings_process('''
from django.conf import settings
assert settings.DATABASES['default']['NAME'] == 'existing_development'
assert settings.APP_REDIS_URL == 'redis://development:6379/5'
assert settings.APP_SESSION_REDIS_URL == 'redis://development:6379/6'
assert settings.SECRET_KEY == 'development-test-secret'
assert settings.JWT_SECRET == 'development-test-jwt'
assert settings.LLM_MODEL == 'development-chat-model'
assert settings.LLM_TRANSLATE_MODEL == 'development-translate-model'
assert settings.LLM_ANALYTICS_MODEL == 'development-analytics-model'
assert settings.LOCATION_PROVIDER == 'google'
assert settings.EVALUATION_LOCATION_PROVIDER == 'emulator'
from studio_desk.celery import app
assert app.conf.broker_url == 'redis://development:6379/4'
assert app.conf.result_backend == settings.CELERY_RESULT_BACKEND
assert settings.EVALUATION_ENABLED
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_non_development_settings(self):
        result = self.settings_process('from django.conf import settings; print(settings.DEBUG)', DEBUG='false')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('DEBUG must be true', result.stderr)

    def test_stale_isolation_urls_cannot_silently_split_runner_from_development(self):
        for variable in ('EVALUATION_DATABASE_URL', 'EVALUATION_REDIS_URL'):
            with self.subTest(variable=variable):
                result = self.settings_process('from django.conf import settings; print(settings.DEBUG)',
                                               **{variable: 'stale-url'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Evaluation now shares development resources', result.stderr)
