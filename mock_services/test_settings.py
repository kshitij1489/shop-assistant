"""Isolated commerce tests without loading unrelated chatbot/dashboard routes."""
from tests.settings.integration import *  # noqa: F403

ROOT_URLCONF = 'commerce.urls'
