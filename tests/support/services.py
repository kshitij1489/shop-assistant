"""Disposable HTTP providers shared by a serial integration test run."""
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep

from mock_services.catalog import load_catalog
from mock_services.client import MockClient
from mock_services.server import make_server


_current_services = ContextVar("integration_mock_services", default=None)


def current_services():
    return _current_services.get()


class MockServices:
    """Own the socket, serving thread and SQLite directory, including failed startup."""

    def __init__(self, *, startup_timeout=5):
        self.startup_timeout = startup_timeout
        self._resources = ExitStack()

    def __enter__(self):
        try:
            directory = self._resources.enter_context(TemporaryDirectory(prefix="studio-tests-"))
            self.directory = Path(directory)
            self.server = make_server(("127.0.0.1", 0), self.directory / "provider.sqlite3", load_catalog())
            # Drain in-flight handlers before removing their SQLite database.
            self.server.daemon_threads = False
            self._resources.callback(self.server.server_close)
            self.thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                                 name="integration-mock-services", daemon=True)
            self.thread.start()
            self._resources.callback(self._stop)
            self.base_url = f"http://127.0.0.1:{self.server.server_port}"
            self.client = MockClient(self.base_url, timeout=min(1, self.startup_timeout))
            self._wait_until_ready()
            return self
        except BaseException:
            self._resources.close()
            raise

    def _wait_until_ready(self):
        deadline = monotonic() + self.startup_timeout
        last_error = None
        while monotonic() < deadline:
            if not self.thread.is_alive():
                raise RuntimeError("Mock HTTP server stopped during startup")
            try:
                self.client.timeout = min(1, max(0.001, deadline - monotonic()))
                health = self.client.request("GET", "/health")
                if health != {"status": "ok", "environment": "test"}:
                    raise ValueError("Unexpected mock provider health response")
                self.client.timeout = 5
                return
            except (OSError, ValueError) as exc:
                last_error = exc
                sleep(min(0.05, max(0, deadline - monotonic())))
        raise TimeoutError("Mock HTTP server did not become ready") from last_error

    def _stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError("Mock HTTP server did not stop")

    def __exit__(self, *exc):
        return self._resources.__exit__(*exc)

    def evidence(self, test):
        """Use the provider's redacted administrative views, never credentials."""
        evidence = {"faults": self.client.request("GET", "/admin/state")}
        for name in ("gateway", "pos"):
            connection = getattr(test, name, None)
            if connection is not None:
                evidence[name] = self.client.request("GET", f"/admin/accounts/{connection.pk}/state")
        account = getattr(test, "location_account", None)
        if account is not None:
            evidence["location"] = self.client.request("GET", f"/admin/location/accounts/{account}/state")
        return evidence


@contextmanager
def service_session():
    with MockServices() as services:
        token = _current_services.set(services)
        try:
            yield services
        finally:
            _current_services.reset(token)
