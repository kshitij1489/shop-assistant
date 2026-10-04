import io
import json
from contextlib import contextmanager
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4
from xml.etree import ElementTree as ET

from mock_services.commerce_adapter.fixtures import payment_command
from mock_services.client import HTTPProvider
from tests.support.cases import MockServicesMixin
from tests.support.reporting import ReportingResultMixin, ReportingTextRunner
from tests.support.runner import IntegrationRunner, ManagedSuite
from tests.support.services import MockServices, current_services, service_session


class HarnessTests(unittest.TestCase):
    def run_suite(self, *cases, failfast=False):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.report = Path(directory)
        self.output = io.StringIO()

        class Result(ReportingResultMixin, unittest.TextTestResult):
            pass

        return ReportingTextRunner(stream=self.output, resultclass=Result, report_dir=directory,
                                   failfast=failfast).run(ManagedSuite(cases))

    def assert_stopped(self, services):
        self.assertFalse(services.thread.is_alive())
        self.assertFalse(services.directory.exists())
        with socket.socket() as probe:
            probe.settimeout(0.2)
            self.assertNotEqual(probe.connect_ex(services.server.server_address), 0)

    def test_server_ready_then_socket_thread_and_directory_are_released(self):
        with MockServices() as services:
            self.assertIsInstance(services.client.request("GET", "/health"), dict)
            self.assertTrue(services.directory.exists())
        self.assert_stopped(services)

    def test_readiness_has_a_deadline(self):
        services = MockServices(startup_timeout=0.02)
        with patch("tests.support.services.MockClient.request", side_effect=OSError("unavailable")):
            with self.assertRaisesRegex(TimeoutError, "did not become ready"):
                with services:
                    self.fail("Startup should not succeed")
        self.assert_stopped(services)

    def test_suite_without_provider_tests_does_not_start_services(self):
        with patch("tests.support.runner.service_session") as start:
            result = self.run_suite(unittest.FunctionTestCase(lambda: None))
        start.assert_not_called()
        self.assertTrue(result.wasSuccessful())

    def test_suite_shares_server_but_provider_accounts_are_isolated(self):
        observed = []
        command = payment_command()

        class First(MockServicesMixin, unittest.TestCase):
            def runTest(self):
                observed.append(self.mock_services)
                provider = HTTPProvider(self.mock_services.client, str(uuid4()), "payment")
                self.assertIsNone(provider.lookup(key=command["idempotency_key"]))
                provider.create(command)
                self.assertIsNotNone(provider.lookup(key=command["idempotency_key"]))

        class Second(First):
            pass

        result = self.run_suite(First(), Second())
        self.assertTrue(result.wasSuccessful(), self.output.getvalue())
        self.assertIs(observed[0], observed[1])
        self.assertIsNone(current_services())
        self.assert_stopped(observed[0])

    def test_class_setup_failure_cleans_up_ide_fallback_server(self):
        observed = []

        class Broken(MockServicesMixin, unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                super().setUpClass()
                observed.append(cls.mock_services)
                raise RuntimeError("fixture failed")

            def runTest(self):
                self.fail("Should not run")

        result = unittest.TestResult()
        unittest.TestSuite([Broken()]).run(result)
        self.assertEqual(len(result.errors), 1)
        self.assert_stopped(observed[0])

    def test_startup_failure_is_an_error_and_prevents_test_execution(self):
        class NeedsProvider(MockServicesMixin, unittest.TestCase):
            def runTest(self):
                raise AssertionError("must not execute")

        with patch("tests.support.runner.service_session", side_effect=OSError("cannot bind")):
            result = self.run_suite(NeedsProvider())
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(len(result.errors), 1)
        suite = ET.parse(self.report / "junit.xml").getroot()
        self.assertEqual(suite.attrib["errors"], "1")
        self.assertIn("cannot bind", suite.find("testcase/error").text)

    def test_teardown_failure_is_an_error_even_when_assertions_pass(self):
        @contextmanager
        def broken_teardown():
            with service_session():
                yield
            raise RuntimeError("cleanup failed")

        class NeedsProvider(MockServicesMixin, unittest.TestCase):
            def runTest(self):
                pass

        with patch("tests.support.runner.service_session", broken_teardown):
            result = self.run_suite(NeedsProvider())
        self.assertFalse(result.wasSuccessful())
        self.assertEqual(result.records[-1]["status"], "ERROR")
        self.assertIn("cleanup failed", result.records[-1]["detail"])

    def test_failfast_collects_evidence_and_stops_services(self):
        observed = []

        class Failing(MockServicesMixin, unittest.TestCase):
            def runTest(self):
                observed.append(self.mock_services)
                self.fail("business assertion")

        result = self.run_suite(Failing(), Failing(), failfast=True)
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(result.records[0]["status"], "FAIL")
        self.assertIn("faults", result.records[0]["evidence"])
        self.assert_stopped(observed[0])

    def test_keyboard_interrupt_still_releases_server_and_writes_report(self):
        observed = []

        class Interrupted(MockServicesMixin, unittest.TestCase):
            def runTest(self):
                observed.append(self.mock_services)
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.run_suite(Interrupted())
        self.assert_stopped(observed[0])
        self.assertEqual(ET.parse(self.report / "junit.xml").getroot().attrib["errors"], "1")

    def test_reports_distinguish_failures_errors_skips_and_expected_failures(self):
        class Outcomes(unittest.TestCase):
            def test_pass(self):
                pass

            def test_fail(self):
                self.fail("bad <result>\x00")

            def test_error(self):
                raise RuntimeError("broken fixture")

            @unittest.skip("not applicable")
            def test_skip(self):
                pass

            @unittest.expectedFailure
            def test_expected(self):
                self.fail("known defect")

            @unittest.expectedFailure
            def test_unexpected(self):
                pass

            def test_subtests(self):
                for number in range(2):
                    with self.subTest(number=number):
                        self.assertEqual(number, 0)

        result = self.run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(Outcomes))
        self.assertFalse(result.wasSuccessful())
        suite = ET.parse(self.report / "junit.xml").getroot()
        self.assertEqual({key: suite.attrib[key] for key in ("tests", "failures", "errors", "skipped")},
                         {"tests": "7", "failures": "3", "errors": "1", "skipped": "2"})
        records = json.loads((self.report / "results.json").read_text())
        self.assertTrue(all(record["duration"] >= 0 for record in records))
        self.assertIn("PASS=1 FAIL=3 ERROR=1 SKIP=2", self.output.getvalue())

    def test_parallel_runner_is_rejected_before_any_resources_start(self):
        with self.assertRaisesRegex(ValueError, "runs serially"):
            IntegrationRunner(parallel=2)

    def test_pre_suite_failure_replaces_stale_success_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junit.xml"
            path.write_text('<testsuite tests="1" failures="0" errors="0"/>')
            runner = IntegrationRunner(report_dir=directory)
            with patch("django.test.runner.DiscoverRunner.run_tests", side_effect=RuntimeError("database unavailable")):
                with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                    runner.run_tests(["tests.integration"])
            report = ET.parse(path).getroot()
            self.assertEqual(report.attrib["errors"], "1")
            self.assertIn("database unavailable", report.find("testcase/error").text)
