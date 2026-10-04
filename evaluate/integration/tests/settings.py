"""SQLite settings with evaluation controls on. No paid calls."""
from evaluate.fixtures.tests.settings import *  # noqa: F403

EVALUATION_ENABLED = True
LOCATION_PROVIDER = "emulator"
