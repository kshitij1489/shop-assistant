"""Summary split: scored PASS vs COMPLETED-unscored vs NEEDS_REVIEW vs real SKIPPED."""
from __future__ import annotations

import threading
import unittest

from evaluate.contracts.models import ExecutionIdentity, ModelNames, RunConfiguration
from evaluate.runner.runner import EvaluationRunner, split_axis_counts
from evaluate.runner.scenario import AttemptResult


def _identity(scenario_id: str, instance: str, attempt: int = 1) -> ExecutionIdentity:
    return ExecutionIdentity(
        run_id="summary-status-run",
        scenario_id=scenario_id,
        scenario_instance_id=instance,
        attempt=attempt,
    )


class SummaryStatusTests(unittest.TestCase):
    def _runner_with(self, attempts: list[AttemptResult]) -> EvaluationRunner:
        runner = EvaluationRunner.__new__(EvaluationRunner)
        runner.config = RunConfiguration(
            run_id="summary-status-run",
            scenario_plan_version="baseline-v1",
            models=ModelNames(chat="chat", translate="translate", analytics="analytics"),
        )
        runner._results = list(attempts)
        runner._results_lock = threading.Lock()
        return runner

    def test_unscored_completion_excluded_from_scored_pass(self):
        """COMPLETED + evaluation_verdict None is not a scored PASS; outcome is not SKIPPED."""
        unscored = AttemptResult(
            _identity("sessions:unscored", "inst-unscored"),
            "COMPLETED",
            execution_status="COMPLETED",
            evaluation_verdict=None,
        )
        scored = AttemptResult(
            _identity("sessions:scored", "inst-scored"),
            "PASS",
            execution_status="COMPLETED",
            evaluation_verdict="PASS",
        )
        summary = self._runner_with([unscored, scored])._summary()
        axes = split_axis_counts(summary.results)

        # Authoritative split: scored PASS only; unscored is its own bucket.
        self.assertEqual(axes["evaluation_verdict"]["PASS"], 1)
        self.assertEqual(axes["evaluation_verdict"]["unscored"], 1)
        self.assertEqual(axes["execution_status"]["COMPLETED"], 2)
        self.assertEqual(axes["execution_status"]["SKIPPED"], 0)

        by_scenario = {row.scenario_id: row for row in summary.results}
        unscored_row = by_scenario["sessions:unscored"]
        self.assertEqual(unscored_row.execution_status, "COMPLETED")
        self.assertIsNone(unscored_row.evaluation_verdict)
        self.assertNotEqual(unscored_row.outcome, "SKIPPED")
        self.assertNotEqual(unscored_row.outcome, "BLOCKED")

        scored_row = by_scenario["sessions:scored"]
        self.assertEqual(scored_row.evaluation_verdict, "PASS")
        self.assertEqual(scored_row.outcome, "PASS")

        # Legacy counts still match stored outcomes (validator); PASS here is not scored-only.
        self.assertEqual(summary.counts["PASS"], 1)
        self.assertEqual(summary.counts["COMPLETED"], 1)
        self.assertEqual(summary.counts.get("SKIPPED", 0), 0)

    def test_needs_review_is_not_skipped_or_scored_pass(self):
        """NEEDS_REVIEW stays off SKIPPED and off evaluation_verdict PASS."""
        needs_review = AttemptResult(
            _identity("sessions:review", "inst-review"),
            "NEEDS_REVIEW",
            execution_status="COMPLETED",
            evaluation_verdict="NEEDS_REVIEW",
        )
        summary = self._runner_with([needs_review])._summary()
        axes = split_axis_counts(summary.results)
        row = summary.results[0]

        self.assertEqual(row.execution_status, "COMPLETED")
        self.assertEqual(row.evaluation_verdict, "NEEDS_REVIEW")
        self.assertNotEqual(row.outcome, "SKIPPED")
        self.assertEqual(axes["evaluation_verdict"]["NEEDS_REVIEW"], 1)
        self.assertEqual(axes["evaluation_verdict"]["PASS"], 0)
        self.assertEqual(axes["execution_status"]["SKIPPED"], 0)
        self.assertEqual(summary.counts.get("SKIPPED", 0), 0)


if __name__ == "__main__":
    unittest.main()
