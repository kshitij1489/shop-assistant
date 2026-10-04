"""Lifecycle tests for EvaluationRunner evidence open, resume, and input artifacts."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluate.evidence import EvidenceConflict
from evaluate.evidence.records import LatencySummary, LoadReport, PhaseMetrics
from evaluate.runner import (
    AwaitOptions, BudgetOptions, Components, EvaluationRunner, LoadOptions, RunnerOptions,
    ProjectionSettlementPolicy, SnapshotBranchOracle, WebsiteTransport,
)
from evaluate.runner.budget import BudgetTracker
from evaluate.runner.runner import CONTRACT_JOURNALS
from evaluate.tests.chat_server import ChatServer
from evaluate.tests.fakes import FakeControls, FakeEvaluator, FakeInspector, FakeProvisioner, make_config, make_scenario


def _empty_phase() -> PhaseMetrics:
    return PhaseMetrics(
        sessions_started=0, sessions_completed=0, requests_sent=0, request_errors=0, session_errors=0,
        latency=LatencySummary(count=0, p50_ms=None, p95_ms=None, max_ms=None),
    )


class RunnerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.server = ChatServer().start()
        self.addCleanup(self.server.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "evidence"
        self.config = make_config(self.server)
        self.provisioner = FakeProvisioner(self.server)
        self.dataset = Path(self.temp.name) / "dataset"
        self.dataset.mkdir()
        (self.dataset / "01_cafe_knowledge.json").write_text(json.dumps({"cafe": {"hours": "open"}}))
        self.config = self.config.model_copy(update={"dataset_directory": str(self.dataset)})

    def components(self) -> Components:
        transport = WebsiteTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        return Components(
            provisioner=self.provisioner, controls=FakeControls(), transport=transport,
            inspector=FakeInspector(self.server), branch_oracle=SnapshotBranchOracle(),
            settlement=ProjectionSettlementPolicy(), evaluator=FakeEvaluator(),
        )

    def test_application_journal_alone_does_not_count_as_prior_run(self):
        self.directory.mkdir(parents=True)
        (self.directory / "application-12345.jsonl").write_text("")
        scenario = make_scenario("fresh", ["hello"])
        scenario = scenario.model_copy(update={"setup_inputs": ["01_cafe_knowledge.json"]})
        outcome = EvaluationRunner(
            self.config, [scenario], self.components(), self.directory,
            RunnerOptions(awaiting=AwaitOptions(enabled=False)),
        ).run()
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        self.assertTrue((self.directory / "scenarios.json").is_file())
        self.assertTrue((self.directory / "knowledge.json").is_file())
        self.assertEqual(json.loads((self.directory / "checks.json").read_text()), [])
        knowledge = json.loads((self.directory / "knowledge.json").read_text())
        self.assertEqual(knowledge[0]["evidence_id"], "knowledge:01_cafe_knowledge")
        self.assertEqual(knowledge[0]["source"], "01_cafe_knowledge.json")
        self.assertEqual(knowledge[0]["content"]["cafe"]["hours"], "open")

    def test_contract_journals_still_conflict_without_resume(self):
        self.directory.mkdir(parents=True)
        (self.directory / "events.jsonl").write_text("")
        with self.assertRaises(EvidenceConflict):
            EvaluationRunner(
                self.config, [make_scenario("x", ["hello"])], self.components(), self.directory,
                RunnerOptions(awaiting=AwaitOptions(enabled=False)),
            ).run()

    def test_interrupted_stop_reason_skips_summary_so_resume_remains_possible(self):
        scenario = make_scenario("interrupt-summary", ["hello"])
        runner = EvaluationRunner(
            self.config, [scenario], self.components(), self.directory,
            RunnerOptions(awaiting=AwaitOptions(enabled=False)),
        )
        interrupted = LoadReport(
            run_id=self.config.run_id, configured_concurrency=1, achieved_concurrency_max=0,
            achieved_concurrency_mean=0.0, ramp_up_seconds=0.0, pacing_seconds=0.0,
            duration_seconds=None, elapsed_seconds=0.1, stop_reason="interrupted",
            warm_up=_empty_phase(), measured=_empty_phase(),
        )

        class FakeScheduler:
            def __init__(self, *args, **kwargs):
                pass

            def run(self, *args, **kwargs):
                return interrupted

        with patch("evaluate.runner.runner.Scheduler", FakeScheduler):
            outcome = runner.run()
        self.assertEqual(outcome.load.stop_reason, "interrupted")
        self.assertFalse((self.directory / "summary.json").exists())
        resumed = EvaluationRunner(
            self.config, [scenario], self.components(), self.directory,
            RunnerOptions(awaiting=AwaitOptions(enabled=False)), resume=True,
        ).run()
        self.assertEqual(resumed.summary.counts["PASS"], 1)
        self.assertTrue((self.directory / "summary.json").exists())

    def test_budget_tracker_seeds_prior_requests_on_resume(self):
        tracker = BudgetTracker(
            BudgetOptions(max_live_calls=3), LoadOptions(max_requests=3),
            starting_requests=2, starting_sessions=1,
        )
        tracker.reserve_request()
        self.assertEqual(tracker.requests, 3)
        with self.assertRaises(Exception):
            tracker.reserve_request()

    def test_contract_journal_names_exclude_application_prefix(self):
        self.assertTrue(all(not name.startswith("application-") for name in CONTRACT_JOURNALS))
        self.assertIn("turns.jsonl", CONTRACT_JOURNALS)
        self.assertIn("dispatch.jsonl", CONTRACT_JOURNALS)


if __name__ == "__main__":
    unittest.main()
