"""Compatibility entry point; application integration tests live under tests/."""


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromName("tests.integration.test_mock_commerce")
