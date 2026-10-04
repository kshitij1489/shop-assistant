"""Local SQLite test database; no startup I/O and no paid calls."""
from tests.settings.integration import *  # noqa: F403
ROOT_URLCONF = 'commerce.urls'
SESSION_ENGINE = 'django.contrib.sessions.backends.db'
