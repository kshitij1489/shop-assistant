"""Evaluation hooks on the development application's database, caches and models.

Used by docker-compose.dev.yml and the runner inside its web container.
Production continues to use studio_desk.settings without evaluation hooks.
"""
import os

from django.core.exceptions import ImproperlyConfigured
from studio_desk.settings import *  # noqa: F403

if not DEBUG:  # noqa: F405
    raise ImproperlyConfigured("Evaluation uses development settings; DEBUG must be true")
if os.environ.get("EVALUATION_DATABASE_URL") or os.environ.get("EVALUATION_REDIS_URL"):
    raise ImproperlyConfigured(
        "Evaluation shares development resources. Remove EVALUATION_DATABASE_URL "
        "and EVALUATION_REDIS_URL; configure the ordinary application settings instead.")

EVALUATION_ENABLED = True
EVALUATION_LOCATION_PROVIDER = os.environ.get("EVALUATION_LOCATION_PROVIDER", "emulator")
if EVALUATION_LOCATION_PROVIDER not in {"emulator", "openstreetmap"}:
    raise ImproperlyConfigured("Evaluation location provider must be emulator or openstreetmap")
EVALUATION_EVIDENCE_DIR = os.environ.get("EVALUATION_EVIDENCE_DIR", "")
EVALUATION_RUN_ID = os.environ.get("EVALUATION_RUN_ID", "")
EVALUATION_EVIDENCE_ROOT = os.environ.get("EVALUATION_EVIDENCE_ROOT", "/var/tmp/studio-eval")
MIDDLEWARE = ["evaluate.integration.health.HealthMiddleware", *MIDDLEWARE]  # noqa: F405
CELERY_BEAT_SCHEDULE = {**CELERY_BEAT_SCHEDULE, "evaluation-heartbeat": {  # noqa: F405
    "task": "evaluate.beat_heartbeat", "schedule": 10.0}}
if "evaluate.integration.apps.IntegrationConfig" not in INSTALLED_APPS:  # noqa: F405
    INSTALLED_APPS = [*INSTALLED_APPS, "evaluate.integration.apps.IntegrationConfig"]  # noqa: F405
