"""Offline application tests with SQLite and disposable HTTP providers.

This profile never loads application settings or developer environment files.
"""
from django.apps import AppConfig

from tests.support.paths import REPOSITORY_ROOT


class ChatbotTestConfig(AppConfig):
    name = "chatbot_core"
    default_auto_field = "django.db.models.BigAutoField"


BASE_DIR = REPOSITORY_ROOT
SECRET_KEY = "offline-tests-only"
OPENAI_API_KEY = "offline-tests-only"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "tests.settings.integration.ChatbotTestConfig",
    "orders",
    "commerce",
    "users",
    "users.apps.OperationsAdminConfig",
    "rest_framework",
]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
# PostgreSQL and deployment migrations run in integration_postgres and CI.
MIGRATION_MODULES = {"chatbot_core": None, "orders": None, "commerce": None, "users": None}
ROOT_URLCONF = "tests.support.urls"
MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
STATIC_URL = "/static/"
LOGIN_URL = "/login/"
CELERY_BROKER_URL = "redis://localhost:6379/15"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
COMMERCE_ADAPTER_SECRETS = {"test-key": "test-adapter-secret"}
TEST_RUNNER = "tests.support.runner.IntegrationRunner"
JWT_SECRET = "integration-tests-only-secret-at-least-32-characters"
PUBLIC_URL = "http://testserver"
SIGNUP_ALERT_EMAIL = ""
LEGACY_TENANT_SYNC_ENABLED = False
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
