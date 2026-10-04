"""Standard unittest outcomes with durations and portable JUnit XML evidence."""
import json
import re
from collections import Counter
from pathlib import Path
from time import perf_counter
import unittest
from xml.etree import ElementTree as ET

from .services import current_services


def xml_text(value):
    # Tracebacks may contain ANSI/control characters forbidden by XML 1.0.
    return re.sub(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]", "", str(value))


class ReportingResultMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started_at = perf_counter()
        self._test_started = self.started_at
        self._first_record = 0

    def startTest(self, test):
        self._test_started = perf_counter()
        self._first_record = len(self.records)
        super().startTest(test)

    def stopTest(self, test):
        elapsed = perf_counter() - self._test_started
        for record in self.records[self._first_record:]:
            record["duration"] = elapsed
        super().stopTest(test)

    def _record(self, test, status, detail="", *, parent=None):
        record = {"id": test.id(), "status": status, "detail": detail, "duration": 0}
        if status in ("FAIL", "ERROR"):
            services = current_services()
            if services is not None:
                try:
                    record["evidence"] = services.evidence(parent or test)
                except Exception as exc:
                    record["evidence"] = {"capture_error": str(exc)}
        self.records.append(record)

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "PASS")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "FAIL", self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "ERROR", self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "SKIP", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "SKIP", "Expected failure:\n" + self._exc_info_to_string(err, test))

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "FAIL", "Unexpected success: remove or update the expected-failure declaration")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            status = "FAIL" if issubclass(err[0], test.failureException) else "ERROR"
            self._record(subtest, status, self._exc_info_to_string(err, test), parent=test)

    def write_report(self, directory):
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        counts = Counter(record["status"] for record in self.records)
        suite = ET.Element("testsuite", name="integration", tests=str(len(self.records)),
                           failures=str(counts["FAIL"]), errors=str(counts["ERROR"]),
                           skipped=str(counts["SKIP"]), time=f"{perf_counter() - self.started_at:.6f}")
        for record in self.records:
            classname, _, name = record["id"].rpartition(".")
            case = ET.SubElement(suite, "testcase", classname=xml_text(classname), name=xml_text(name),
                                 time=f"{record['duration']:.6f}")
            element = {"FAIL": "failure", "ERROR": "error", "SKIP": "skipped"}.get(record["status"])
            if element:
                ET.SubElement(case, element).text = xml_text(record["detail"])
            if "evidence" in record:
                ET.SubElement(case, "system-out").text = xml_text(json.dumps(record["evidence"], indent=2))
        ET.indent(suite)
        ET.ElementTree(suite).write(directory / "junit.xml", encoding="utf-8", xml_declaration=True)
        (directory / "results.json").write_text(json.dumps(self.records, indent=2) + "\n", encoding="utf-8")
        self.stream.writeln("Integration outcomes: " + " ".join(
            f"{status}={counts[status]}" for status in ("PASS", "FAIL", "ERROR", "SKIP")))
        self.stream.writeln(f"Reports: {directory / 'junit.xml'}")


class ReportingTextRunner(unittest.TextTestRunner):
    def __init__(self, *args, report_dir, **kwargs):
        super().__init__(*args, **kwargs)
        self.report_dir = report_dir

    def _makeResult(self):
        self.integration_result = super()._makeResult()
        return self.integration_result

    def run(self, test):
        try:
            return super().run(test)
        finally:
            if hasattr(self, "integration_result"):
                self.integration_result.write_report(self.report_dir)
