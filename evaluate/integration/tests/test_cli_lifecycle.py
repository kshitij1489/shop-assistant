"""CLI lifecycle coverage for live selection, workload, resume, and subcommands."""
from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from evaluate.__main__ import main
from evaluate.contracts.models import Issue
from evaluate.integration import command as command_module
from evaluate.integration.command import _apply_workload, _runner_options, _select, stitch_report
from evaluate.runner.options import LoadOptions, RunnerOptions
from evaluate.scenarios.plan import PLAN_PATH
from evaluate.tests.fakes import make_scenario

ROOT = Path(__file__).resolve().parents[3]


class CliLifecycleTests(unittest.TestCase):
    def test_validate_defaults_to_execution_plan_and_includes_readiness(self):
        stdout = io.StringIO()
        with patch("sys.stdout", stdout):
            code = main(["validate", "--dataset", str(ROOT / "test_data")])
        self.assertEqual(code, 0)
        report = json.loads(stdout.getvalue())
        self.assertIn("readiness", report)
        self.assertEqual(report["scenario_plan_version"], "dataset-setup-v2-text-address")
        self.assertEqual(report["readiness"], {})
        self.assertEqual(report["ready_scenarios"], 219)
        self.assertEqual(report["blocked_scenarios"], 0)
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
            self.assertEqual(main(["validate", "--dataset", str(ROOT / "test_data"), "--require-ready"]), 0)

    def test_validate_plan_default_path_is_execution_plan(self):
        self.assertEqual(PLAN_PATH.name, "execution.plan.json")
        parser_help = io.StringIO()
        with patch("sys.stdout", parser_help), patch("sys.stderr", parser_help):
            try:
                main(["validate", "--help"])
            except SystemExit:
                pass
        self.assertIn("execution.plan.json", parser_help.getvalue())

    def test_select_keeps_blocked_scenarios_with_runtime_blockers(self):
        ready = make_scenario("ready", ["hello"])
        blocked = make_scenario("blocked", ["hello"], blockers=True)
        # readiness-only blocker via duplicate fixture path is covered elsewhere; dataset blockers stay.
        selected = _select([ready, blocked], set())
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual([s.scenario_id for s in selected], [ready.scenario_id, blocked.scenario_id])
        blocked_selected = next(s for s in selected if s.scenario_id == blocked.scenario_id)
        self.assertTrue(blocked_selected.blockers)
        self.assertTrue(all(isinstance(issue, Issue) for issue in blocked_selected.blockers))

    def test_explicit_blocked_scenario_stays_selected(self):
        blocked = make_scenario("blocked", ["hello"], blockers=True)
        selected = _select([blocked], {blocked.scenario_id})
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(len(selected), 1)
        self.assertTrue(selected[0].blockers)

    def test_smoke_workload_caps_scenarios_and_budgets(self):
        scenarios = [make_scenario(f"s{i}", ["hello"]) for i in range(8)]
        options = RunnerOptions()
        capped = _apply_workload("smoke", scenarios, options)
        self.assertEqual(len(capped), 5)
        self.assertEqual(options.load.concurrency, 1)
        self.assertEqual(options.load.max_requests, 40)
        self.assertEqual(options.budget.max_live_calls, 40)
        full = _apply_workload("full", scenarios, RunnerOptions(load=LoadOptions(concurrency=4)))
        self.assertEqual(len(full), 8)

    def test_runner_options_from_cli_flags(self):
        args = argparse.Namespace(
            recovery_attempts=3, await_turns=False, max_wait=5.0, poll_interval=0.25,
            branch_mismatch="continue_flagged", concurrency=2, ramp_up=1.5, pacing=0.1,
            duration=30.0, warm_up_sessions=1, max_sessions=10, max_requests=50,
            max_provider_sessions=2, max_live_calls=40, max_estimated_cost=1000, cost_per_million_tokens_minor=100,
        )
        options = _runner_options(args)
        self.assertEqual(options.recovery.max_attempts, 3)
        self.assertFalse(options.awaiting.enabled)
        self.assertEqual(options.awaiting.max_wait_seconds, 5.0)
        self.assertEqual(options.branching.on_mismatch, "continue_flagged")
        self.assertEqual(options.load.concurrency, 2)
        self.assertEqual(options.load.duration_seconds, 30.0)
        self.assertEqual(options.budget.max_live_calls, 40)

    def test_runner_document_supplies_workload_controls(self):
        args = argparse.Namespace(
            recovery_attempts=None, await_turns=None, max_wait=None, poll_interval=None,
            branch_mismatch=None, concurrency=None, ramp_up=None, pacing=None,
            duration=None, warm_up_sessions=None, max_sessions=None, max_requests=None,
            max_provider_sessions=None, max_live_calls=None, max_estimated_cost=None,
            runner_doc={"concurrency": 3, "max_live_calls": 12, "max_requests": 15},
        )
        options = _runner_options(args)
        self.assertEqual(options.load.concurrency, 3)
        self.assertEqual(options.load.max_requests, 15)
        self.assertEqual(options.budget.max_live_calls, 12)
        args.concurrency = 2
        self.assertEqual(_runner_options(args).load.concurrency, 2)

    def test_https_adapter_path_includes_commerce_mount(self):
        from evaluate.scenarios.runtime import commerce_adapter_url
        url = commerce_adapter_url("https://127.0.0.1:8443", "abc", "commands/claim/")
        self.assertEqual(
            url, "https://127.0.0.1:8443/commerce/v1/connections/abc/commands/claim/",
        )

    def test_live_requires_workload(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({
                "schema_version": "1.0.0", "run_id": "workload-run",
                "base_url": "http://127.0.0.1:8000",
                "dataset_directory": str(ROOT / "test_data"),
                "scenario_plan_version": "dataset-setup-v2-text-address",
                "models": {"chat": "gpt-6-luna", "translate": "gpt-6-luna", "analytics": "gpt-6-luna"},
            }))
            args = argparse.Namespace(
                allow_live_chat=True, dataset=ROOT / "test_data", config=config,
                output=Path(temp) / "evidence", scenario=[], cache="cold", workload=None,
            )
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                code = command_module.execute(args)
            self.assertEqual(code, 2)
            self.assertIn("workload", stderr.getvalue())

    def test_stitch_report_mentions_blocked_without_live_chat(self):
        report = stitch_report(ROOT / "test_data")
        self.assertFalse(report["live_chat"])
        self.assertEqual(report["blocked_scenarios"], [])

    def test_score_and_report_subcommands_delegate(self):
        with patch("evaluate.reports.__main__.main", return_value=0) as reports_main:
            code = main(["score", "/tmp/artifacts", "--output", "/tmp/out"])
            self.assertEqual(code, 0)
            self.assertEqual(reports_main.call_args.args[0][0], "score")
            code = main(["report", "/tmp/report.json", "--output", "/tmp/out"])
            self.assertEqual(code, 0)
            self.assertEqual(reports_main.call_args.args[0][:2], ["render", "/tmp/report.json"])

    def test_preflight_subcommand_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text(json.dumps({
                "schema_version": "1.0.0", "run_id": "preflight-run",
                "base_url": "http://127.0.0.1:8000", "dataset_directory": str(ROOT / "test_data"),
                "scenario_plan_version": "dataset-setup-v2-text-address",
                "models": {"chat": "gpt-6-luna", "translate": "gpt-6-luna", "analytics": "gpt-6-luna"},
            }))
            output = Path(temp) / "evidence"
            stdout = io.StringIO()
            stderr = io.StringIO()
            env_report = {
                "schema_version": "1.0.0",
                "valid": True,
                "blockers": [],
                "checks": {"evaluation_enabled": {"ok": True}},
            }
            with patch("django.setup"), \
                 patch("evaluate.integration.preflight.run_preflight", return_value=env_report) as run_pf, \
                 patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                code = main(["preflight", "--dataset", str(ROOT / "test_data"),
                             "--config", str(config), "--output", str(output)])
            self.assertEqual(code, 0)
            run_pf.assert_called_once()
            kwargs = run_pf.call_args.kwargs
            self.assertEqual(kwargs["base_url"], "http://127.0.0.1:8000")
            self.assertEqual(Path(kwargs["evidence_directory"]), output)
            payload = stdout.getvalue() or stderr.getvalue()
            report = json.loads(payload)
            self.assertTrue(report["valid"])
            self.assertNotIn("readiness", report)
            self.assertIn("checks", report)

if __name__ == "__main__":
    unittest.main()
