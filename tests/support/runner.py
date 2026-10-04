"""Django runner that owns provider lifetime and preserves unittest semantics."""
from contextlib import ExitStack
import sys
import unittest

from django.test.runner import DiscoverRunner
from django.test.utils import iter_test_cases

from .reporting import ReportingResultMixin, ReportingTextRunner
from .services import service_session


class InfrastructureTest(unittest.TestCase):
    def __init__(self, phase):
        super().__init__()
        self.phase = phase

    def id(self):
        return f"integration.infrastructure.{self.phase}"

    def __str__(self):
        return self.id()

    def runTest(self):
        pass


def infrastructure_error(result, phase, error):
    test = InfrastructureTest(phase)
    result.startTest(test)
    try:
        result.addError(test, error)
    finally:
        result.stopTest(test)


class ManagedSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        resources = ExitStack()
        try:
            if any(getattr(test, "requires_mock_services", False) for test in iter_test_cases(self)):
                try:
                    resources.enter_context(service_session())
                except Exception:
                    infrastructure_error(result, "startup", sys.exc_info())
                    return result
            return super().run(result, debug=debug)
        except KeyboardInterrupt:
            infrastructure_error(result, "interrupted", sys.exc_info())
            raise
        finally:
            try:
                resources.close()
            except Exception:
                infrastructure_error(result, "teardown", sys.exc_info())


class IntegrationRunner(DiscoverRunner):
    test_runner = ReportingTextRunner

    @classmethod
    def add_arguments(cls, parser):
        super().add_arguments(parser)
        parser.add_argument("--report-dir", default=".test-reports/integration",
                            help="Directory for junit.xml and results.json (overwritten each run).")

    def __init__(self, *args, report_dir=".test-reports/integration", **kwargs):
        super().__init__(*args, **kwargs)
        if self.parallel > 1:
            raise ValueError("The integration harness runs serially; omit --parallel or use --parallel=1.")
        self.report_dir = report_dir

    def get_resultclass(self):
        base = super().get_resultclass() or unittest.TextTestResult
        return type("IntegrationResult", (ReportingResultMixin, base), {})

    def get_test_runner_kwargs(self):
        return {**super().get_test_runner_kwargs(), "report_dir": self.report_dir}

    def run_suite(self, suite, **kwargs):
        runner = self.test_runner(**self.get_test_runner_kwargs())
        try:
            return runner.run(ManagedSuite([suite]))
        finally:
            self.report_result = getattr(runner, "integration_result", None)
            if self._shuffler is not None:
                self.log(f"Used shuffle seed: {self._shuffler.seed_display}")

    def run_tests(self, test_labels, **kwargs):
        self.report_result = None
        try:
            return super().run_tests(test_labels, **kwargs)
        except (Exception, KeyboardInterrupt):
            # Discovery, database setup/checks and database teardown can fail
            # outside run_suite(). They must replace any stale success report.
            if self.report_result is None:
                runner = self.test_runner(**self.get_test_runner_kwargs())
                self.report_result = runner._makeResult()
            interrupted = any(record["id"] == "integration.infrastructure.interrupted"
                              for record in self.report_result.records)
            if not interrupted:
                infrastructure_error(self.report_result, "runner", sys.exc_info())
            self.report_result.write_report(self.report_dir)
            raise
