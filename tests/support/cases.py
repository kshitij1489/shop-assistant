"""Opt-in provider infrastructure for Django or unittest test cases."""
from .services import MockServices, current_services


class MockServicesMixin:
    """Request the run's providers; individual IDE runs get a class-owned server."""

    requires_mock_services = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mock_services = current_services()
        if cls.mock_services is None:
            resources = MockServices()
            # Register before entering so even partial startup is cleaned up.
            cls.addClassCleanup(resources.__exit__, None, None, None)
            cls.mock_services = resources.__enter__()
