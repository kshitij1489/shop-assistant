"""Integration profile with PostgreSQL and the real application migrations."""
import os

from .integration import *  # noqa: F403

DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": "postgres",
    "HOST": os.environ.get("COMMERCE_TEST_PG_HOST", "127.0.0.1"),
    "PORT": os.environ.get("COMMERCE_TEST_PG_PORT", "5432"),
    "USER": os.environ.get("COMMERCE_TEST_PG_USER", ""),
    "TEST": {"NAME": "test_studio_desk_commerce"},
}}
MIGRATION_MODULES = {}
