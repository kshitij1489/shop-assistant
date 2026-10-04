from copy import deepcopy
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from evaluate.contracts.models import ExecutionEvent
from evaluate.checks.models import CheckSpec
from evaluate.reports.__main__ import main
from evaluate.reports.artifacts import Diagnostic, RunArtifacts, load_artifacts
from evaluate.reports.example import example_report, synthetic_run
from evaluate.reports.render import csv_text, dumps, html_report, write_report
from evaluate.reports.scoring import compare_runs, evaluate_run


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.bundle = synthetic_run()
        self.report, self.decisions = example_report(self.bundle)

    def test_four_outcomes_with_denominators_and_coverage(self):
        sessions = self.report["overall"]["sessions"]
        self.assertEqual(sessions["denominator"], 4)
        self.assertEqual(sessions["counts"], {"PASS": 1, "FAIL": 1, "BLOCKED": 1, "NEEDS_REVIEW": 1})
        self.assertEqual(sessions["pass_rate"], .25)
        self.assertEqual(sessions["decided_denominator"], 2)
        self.assertEqual(set(self.report["coverage"]), {"profile", "intent", "priority", "dataset"})
        self.assertEqual(self.report["workload_metrics"]["latency_ms"]["samples"], 4)

    def test_html_escapes_all_untrusted_content_and_links_evidence(self):
        rendered = html_report(self.report)
        self.assertNotIn('<script>alert("unsafe")</script>', rendered)
        self.assertIn('&lt;script&gt;alert', rendered)
        self.assertIn('type="search"', rendered)
        self.assertIn('href="#e-', rendered)
        self.assertIn("response-fail", rendered)
        self.assertIn("after-fail", rendered)

    def test_csv_guards_formula_injection(self):
        rendered = csv_text([{"criterion": "=HYPERLINK(\"bad\")"}], ["criterion"])
        self.assertIn("'=HYPERLINK", rendered)

    def test_append_only_reports_and_immutable_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "artifacts.json"
            source.write_text(dumps(self.bundle.model_dump()))
            before = source.read_bytes()
            loaded = load_artifacts(source)
            first = write_report(evaluate_run(loaded), Path(root) / "evaluations")
            second = write_report(evaluate_run(loaded), Path(root) / "evaluations")
            self.assertNotEqual(first, second)
            old = (first / "report.json").read_bytes()
            with self.assertRaises(FileExistsError):
                write_report(json.loads(old), Path(root) / "evaluations")
            self.assertEqual((first / "report.json").read_bytes(), old)
            self.assertEqual(source.read_bytes(), before)

    def test_offline_scoring_and_regeneration_never_open_sockets(self):
        with tempfile.TemporaryDirectory() as root, patch.object(socket.socket, "connect", side_effect=AssertionError("Network prohibited")):
            source = Path(root) / "artifacts.json"
            source.write_text(dumps(self.bundle.model_dump()))
            output = Path(root) / "evaluations"
            self.assertEqual(main(["score", str(source), "--output", str(output)]), 0)
            first = next(output.glob("*/report.json"))
            self.assertEqual(main(["render", str(first), "--output", str(output)]), 0)
            self.assertEqual(len(list(output.glob("*/report.html"))), 2)

    def test_deterministic_rescore_ignores_new_evaluation_id(self):
        a, b = evaluate_run(self.bundle), evaluate_run(self.bundle)
        for key in ("assertions", "sessions", "turns", "overall", "workload_metrics", "provenance"):
            self.assertEqual(a[key], b[key])
        self.assertNotEqual(a["evaluation_id"], b["evaluation_id"])

    def test_missing_planned_turns_stay_in_denominator(self):
        self.bundle.turns = []
        report = evaluate_run(self.bundle)
        self.assertEqual(report["overall"]["turns"]["denominator"], 4)
        self.assertEqual(report["overall"]["turns"]["counts"]["BLOCKED"], 4)
        self.assertEqual(sum(r["item_kind"] == "expected_facts" for r in report["assertions"]), 4)

    def _empty_rejection_bundle(self, *, mutate_first=False, recover=True,
                                unavailable_first=False):
        bundle = synthetic_run()
        scenario = bundle.scenarios[0]
        template = scenario.turns[0]
        sources = [
            template.model_copy(update={"original_turn_index": 0, "user_turn_index": 0, "text": "",
                "allow_empty_rejection": True,
                "expected_facts": ["Reject empty input without changing state."],
                "must_not": ["Do not invent a request."]}),
            template.model_copy(update={"original_turn_index": 1, "user_turn_index": 1, "text": "  \n\t",
                "allow_empty_rejection": True,
                "expected_facts": ["Reject whitespace without changing state."],
                "must_not": ["Do not create an order."]}),
            template.model_copy(update={"original_turn_index": 2, "user_turn_index": 2,
                "text": "What time do you open?", "expected_facts": ["Answer the valid question."],
                "must_not": ["Do not remain stuck."]}),
        ]
        bundle.scenarios[0] = scenario.model_copy(update={"turns": sources if recover else sources[:2],
                                                          "setup": scenario.setup.model_copy(update={"payment": "unavailable"}),
                                                          "setup_profiles": ["knowledge_only"]})
        template_turn = bundle.turns[0]
        blank_turns = []
        snapshots = []
        base_snapshot = next(row for row in bundle.snapshots
                             if row.scenario_id == scenario.scenario_id and row.phase == "before")
        baseline = deepcopy(base_snapshot.state)
        baseline.update({
            "chat": {"id": "chat-empty", "completed": False}, "checkout": {},
            "address_selection": {}, "commands": [], "reconciliation": [],
            "payment": {"status": "not_requested"}, "pos": {"status": "not_requested"},
            "pending_async": [],
        })
        for index, message in enumerate(("", "  \n\t")):
            request_id = f"request-empty-{index}"
            before_id, after_id = f"before-empty-{index}", f"after-empty-{index}"
            blank_turns.append(template_turn.model_copy(update={
                "event_id": f"turn-empty-{index}", "original_turn_index": index,
                "user_turn_index": index, "request_id": request_id, "message": message,
                "sent_message": message, "response_text": None, "response_error": "Missing message",
                "http_status": 400, "snapshot_ids": [before_id, after_id],
            }))
            after_state = deepcopy(baseline)
            if mutate_first and index == 0:
                after_state["commands"] = [{"kind": "unexpected"}]
            for phase, snapshot_id, state in (("before", before_id, baseline),
                                               ("after", after_id, after_state)):
                snapshots.append(base_snapshot.model_copy(update={
                    "event_id": f"event-{snapshot_id}", "snapshot_id": snapshot_id,
                    "original_turn_index": index, "request_id": request_id, "phase": phase,
                    "captured_at": f"2026-09-28T06:30:0{index * 2 + (phase == 'after')}+00:00",
                    "state": deepcopy(state),
                    "unavailable_sections": (["basket", "provider_receipts"]
                                             if unavailable_first and index == 0 else ["provider_receipts"]),
                }))
        recovery = template_turn.model_copy(update={
            "event_id": "turn-recovery", "original_turn_index": 2, "user_turn_index": 2,
            "request_id": "request-recovery", "message": "What time do you open?",
            "sent_message": "What time do you open?", "response_text": "We open at noon.",
            "response_error": None, "http_status": 200, "snapshot_ids": [],
        })
        bundle.turns = [*blank_turns, *([recovery] if recover else []),
                        *[row for row in bundle.turns if row.scenario_id != scenario.scenario_id]]
        bundle.snapshots = [*snapshots,
                            *[row for row in bundle.snapshots if row.scenario_id != scenario.scenario_id]]
        bundle.checks = [row for row in bundle.checks if row.scenario_id != scenario.scenario_id]
        return bundle, scenario.scenario_id

    def test_controlled_empty_rejections_pass_when_state_is_unchanged_and_chat_recovers(self):
        bundle, scenario_id = self._empty_rejection_bundle()
        report = evaluate_run(bundle)
        corrected = [row for row in report["assertions"]
                     if row["scenario_id"] == scenario_id and row["original_turn_index"] in {0, 1}]
        self.assertEqual(len(corrected), 6)
        self.assertEqual({row["outcome"] for row in corrected}, {"PASS"})
        self.assertEqual(report["workload_metrics"]["http_errors"], 0)
        self.assertEqual(next(row["outcome"] for row in report["sessions"]
                              if row["scenario_id"] == scenario_id), "NEEDS_REVIEW")

    def _assert_first_rejection_blocked(self, bundle, scenario_id):
        report = evaluate_run(bundle)
        rows = [row for row in report["assertions"] if row["scenario_id"] == scenario_id
                and row["original_turn_index"] == 0 and row["item_kind"] != "check"]
        self.assertTrue(rows)
        self.assertEqual({row["outcome"] for row in rows}, {"BLOCKED"})

    def test_empty_rejection_requires_explicit_local_evidence_on_both_sides(self):
        for path in ("basket/items", "orders", "payments", "commands", "effects", "addresses",
                     "reconciliation", "chat/id", "chat/completed", "checkout", "address_selection",
                     "payment/status", "pos/status", "pending_async"):
            for phases in (("before",), ("after",), ("before", "after")):
                for malformed in (False, True):
                    with self.subTest(path=path, phases=phases, malformed=malformed):
                        bundle, sid = self._empty_rejection_bundle()
                        for row in bundle.snapshots:
                            if row.scenario_id == sid and row.original_turn_index == 0 and row.phase in phases:
                                target = row.state
                                *parents, key = path.split("/")
                                for parent in parents:
                                    target = target[parent]
                                if malformed:
                                    target[key] = None
                                else:
                                    del target[key]
                        self._assert_first_rejection_blocked(bundle, sid)

    def test_empty_rejection_does_not_ignore_other_unavailable_sections(self):
        for section in ("chat", "payments", "commands", "unknown_section"):
            for phase in ("before", "after"):
                with self.subTest(section=section, phase=phase):
                    bundle, sid = self._empty_rejection_bundle()
                    for row in bundle.snapshots:
                        if row.scenario_id == sid and row.original_turn_index == 0 and row.phase == phase:
                            row.unavailable_sections.append(section)
                    self._assert_first_rejection_blocked(bundle, sid)

    def test_empty_rejection_compares_all_available_state(self):
        mutations = {"basket": {"items": [{"name": "soup", "quantity": 1}]},
                     "orders": [{"id": "unexpected"}], "payments": [{"status": "pending"}],
                     "chat": {"id": "chat-empty", "completed": True},
                     "provider_receipts": [{"id": "unexpected"}], "quote": {"id": "unexpected"}}
        for section, value in mutations.items():
            with self.subTest(section=section):
                bundle, sid = self._empty_rejection_bundle()
                for row in bundle.snapshots:
                    if row.scenario_id == sid and row.original_turn_index == 0 and row.phase == "after":
                        row.state[section] = value
                self._assert_first_rejection_blocked(bundle, sid)

    def test_empty_rejection_requires_receipts_for_provider_scenarios_and_checks(self):
        for requirement in ("profile", "adapter", "receipts_check", "payment_check", "pos_check"):
            with self.subTest(requirement=requirement):
                bundle, sid = self._empty_rejection_bundle()
                scenario = bundle.scenarios[0]
                if requirement == "profile":
                    scenario.setup_profiles = ["catalog_sandbox"]
                elif requirement == "adapter":
                    scenario.setup.payment = "fake_adapter"
                else:
                    kind = {"receipts_check": "unchanged", "payment_check": "payment",
                            "pos_check": "pos_acceptance"}[requirement]
                    # A later provider check also prevents the local-only exemption.
                    bundle.checks.append(CheckSpec(check_id="provider-required", scenario_id=sid,
                        original_turn_index=2, kind=kind,
                        path="provider_receipts" if requirement == "receipts_check" else "",
                        criterion="Provider evidence is required."))
                self._assert_first_rejection_blocked(bundle, sid)

    def test_empty_rejection_preserves_exact_transport_and_opt_in_requirements(self):
        changes = ({"http_status": 500}, {"response_error": "Different error"},
                   {"transport_error": "timeout"}, {"branch": "mismatch"})
        for change in changes:
            with self.subTest(change=change):
                bundle, sid = self._empty_rejection_bundle()
                bundle.turns[0] = bundle.turns[0].model_copy(update=change)
                self._assert_first_rejection_blocked(bundle, sid)
        bundle, sid = self._empty_rejection_bundle()
        bundle.scenarios[0].turns[0].allow_empty_rejection = False
        self._assert_first_rejection_blocked(bundle, sid)

    def test_empty_rejection_does_not_pass_when_state_changes(self):
        bundle, scenario_id = self._empty_rejection_bundle(mutate_first=True)
        report = evaluate_run(bundle)
        first = [row for row in report["assertions"]
                 if row["scenario_id"] == scenario_id and row["original_turn_index"] == 0]
        self.assertEqual(len(first), 3)
        self.assertEqual({row["outcome"] for row in first}, {"BLOCKED"})

    def test_empty_rejection_does_not_pass_when_same_section_is_unavailable(self):
        bundle, scenario_id = self._empty_rejection_bundle(unavailable_first=True)
        report = evaluate_run(bundle)
        first = [row for row in report["assertions"]
                 if row["scenario_id"] == scenario_id and row["original_turn_index"] == 0]
        self.assertEqual(len(first), 3)
        self.assertEqual({row["outcome"] for row in first}, {"BLOCKED"})

    def test_empty_rejection_does_not_pass_without_later_recovery(self):
        bundle, scenario_id = self._empty_rejection_bundle(recover=False)
        report = evaluate_run(bundle)
        corrected = [row for row in report["assertions"] if row["scenario_id"] == scenario_id]
        self.assertEqual(len(corrected), 6)
        self.assertEqual({row["outcome"] for row in corrected}, {"BLOCKED"})

    def test_no_measurement_is_null_not_zero_cost(self):
        self.bundle.measurements = []
        report = evaluate_run(self.bundle)
        self.assertIsNone(report["workload_metrics"]["input_tokens"]["total"])
        self.assertEqual(report["workload_metrics"]["cost"], {})
        self.assertIsNone(report["workload_metrics"]["api_errors"])

    def test_comparison_flags_incompatible_data_configuration_and_rubric(self):
        current = deepcopy(self.report)
        current["provenance"]["manifest"]["dataset_hashes"] = {"changed": "0" * 64}
        current["provenance"]["manifest"]["configuration_hash"] = "0" * 64
        current["provenance"]["rubric_version"] = "new"
        diff = compare_runs(self.report, current)
        self.assertFalse(diff["compatible"])
        self.assertEqual(set(diff["incompatibilities"]), {"dataset_hashes", "configuration_hash", "rubric_version"})

    def test_duplicate_or_cross_execution_artifacts_are_rejected(self):
        raw = self.bundle.model_dump()
        raw["turns"].append(raw["turns"][0])
        with self.assertRaises(ValueError):
            RunArtifacts.model_validate(raw)
        raw = self.bundle.model_dump()
        raw["turns"][0]["snapshot_ids"] = ["after-fail"]
        with self.assertRaises(ValueError):
            RunArtifacts.model_validate(raw)

    def test_crash_evidence_and_errors_are_blocked_not_behavioral_failures(self):
        first = self.bundle.turns[0]
        self.bundle.events.append(ExecutionEvent(run_id=first.run_id, scenario_id=first.scenario_id,
                scenario_instance_id=first.scenario_instance_id, attempt=1, event_id="crash", occurred_at="2026-09-28T06:30:02+00:00",
                kind="error", request_id=first.request_id, original_turn_index=0, status="failed", detail="Redacted worker crash: synthetic."))
        report = evaluate_run(self.bundle)
        self.assertIn("crash", html_report(report))
        self.assertEqual(next(r for r in report["assertions"] if r["assertion_id"] == "crash:execution")["outcome"], "BLOCKED")

    def test_directory_adapter(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            raw = self.bundle.model_dump()
            (root / "run_manifest.json").write_text(dumps(raw.pop("manifest")))
            raw.pop("evaluation_input_version")
            for name, values in raw.items():
                (root / f"{name}.json").write_text(dumps(values))
            self.assertEqual(load_artifacts(root), self.bundle)

    def test_runner_journals_crashes_usage_and_truncated_tail(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            raw = self.bundle.model_dump()
            (root / "manifest.json").write_text(dumps(raw["manifest"]))
            (root / "scenarios.json").write_text(dumps(raw["scenarios"]))
            for key in ("turns", "snapshots", "events"):
                (root / f"{key}.jsonl").write_text(''.join(json.dumps(row) + '\n' for row in raw[key]))
            for key in ("checks", "knowledge"):
                (root / f"{key}.json").write_text(dumps(raw[key]))
            with (root / "events.jsonl").open("a") as stream:
                stream.write('{"event_id": "incomplete"')
            (root / "crashes").mkdir()
            identity = {k: raw["turns"][0][k] for k in ("run_id", "scenario_id", "scenario_instance_id", "attempt", "request_id", "original_turn_index")}
            (root / "crashes" / "crash.json").write_text(dumps({**identity, "traceback": ["Synthetic stack frame"]}))
            (root / "reports").mkdir()
            (root / "reports" / "budget.json").write_text(dumps({"run_id": "synthetic-run", "actual": {"tokens": 200, "cost_minor": 2}, "estimated": {"tokens": 100}}))
            original = (root / "events.jsonl").read_bytes()
            bundle = load_artifacts(root)
            report = evaluate_run(bundle)
            self.assertEqual((root / "events.jsonl").read_bytes(), original)
            self.assertIn("Synthetic stack frame", html_report(report))
            self.assertEqual(report["workload_metrics"]["runner_reports"]["reports/budget.json"]["actual"]["tokens"], 200)
            self.assertTrue(any(r["category"] == "execution" and r["outcome"] == "BLOCKED" for r in report["assertions"]))

    def test_crash_only_attempt_is_kept_in_denominator(self):
        turn = self.bundle.turns[0]
        self.bundle.diagnostics.append(Diagnostic(evidence_id="crash:second", kind="crash", source="crashes/second.json",
                data={"run_id": turn.run_id, "scenario_id": turn.scenario_id, "scenario_instance_id": turn.scenario_instance_id,
                      "attempt": 2, "traceback": ["Crash before first turn"]}))
        report = evaluate_run(self.bundle)
        self.assertEqual(report["overall"]["sessions"]["denominator"], 5)
        self.assertEqual(next(s for s in report["sessions"] if s["attempt"] == 2)["outcome"], "BLOCKED")

    def test_auto_claim_never_passes_on_an_unrelated_change(self):
        self.bundle.turns[0].response_text = "I saved your address."
        self.bundle.snapshots[1].state["addresses"] = [{"id": "some-other-address"}]
        report = evaluate_run(self.bundle)
        claim = next(r for r in report["assertions"] if r["assertion_id"] == "request-pass:check:auto-claim-0")
        self.assertEqual(claim["outcome"], "NEEDS_REVIEW")

    def test_auto_claim_absent_effect_fails_but_idempotent_ack_needs_review(self):
        self.bundle.turns[0].response_text = "Your order has been placed."
        report = evaluate_run(self.bundle)
        self.assertEqual(next(r for r in report["assertions"] if r["assertion_id"] == "request-pass:check:auto-claim-0")["outcome"], "FAIL")
        for snap in self.bundle.snapshots[:2]:
            snap.state["orders"] = [{"id": "existing-order"}]
        report = evaluate_run(self.bundle)
        self.assertEqual(next(r for r in report["assertions"] if r["assertion_id"] == "request-pass:check:auto-claim-0")["outcome"], "NEEDS_REVIEW")

    def test_rendering_untrusted_saved_numeric_fields_is_escaped(self):
        malicious = '<img src=x onerror=alert(1)>'
        self.report["assertions"][0]["attempt"] = malicious
        self.report["overall"]["assertions"]["denominator"] = malicious
        rendered = html_report(self.report)
        self.assertNotIn(malicious, rendered)
        self.assertIn('&lt;img', rendered)

    def test_scoring_uses_runner_before_snapshot_and_flags_redaction(self):
        self.bundle.snapshots[0].request_id = None
        self.bundle.turns[0].snapshot_ids.append(self.bundle.snapshots[0].snapshot_id)
        self.bundle.turns[0].message = "[REDACTED]"
        loaded = RunArtifacts.model_validate(self.bundle.model_dump())
        report = evaluate_run(loaded)
        self.assertTrue(any(r["assertion_id"] == "request-pass:input_difference" for r in report["assertions"]))
        self.assertFalse(any(r["assertion_id"] == "request-pass:missing_state" for r in report["assertions"]))

    def test_warm_up_excluded_from_scored_denominators(self):
        turn = self.bundle.turns[0]
        self.bundle.diagnostics.append(Diagnostic(
            evidence_id="dispatch:warm", kind="dispatch", source="dispatch.jsonl",
            data={"request_id": turn.request_id, "warm_up": True, "status": "completed",
                  "run_id": turn.run_id, "scenario_id": turn.scenario_id,
                  "scenario_instance_id": turn.scenario_instance_id, "attempt": turn.attempt}))
        if "warm_up" in getattr(type(turn), "model_fields", {}):
            self.bundle.turns[0] = turn.model_copy(update={"warm_up": True})
        report = evaluate_run(self.bundle)
        self.assertEqual(report["workload_metrics"]["latency_ms"]["samples"], 3)
        self.assertEqual(report["workload_metrics"]["request_denominator"], 3)
        self.assertEqual(report["overall"]["sessions"]["denominator"], 3)
        self.assertEqual(report["workload_metrics"]["warm_up"]["request_denominator"], 1)

    def test_compatible_when_only_run_id_differs(self):
        current = deepcopy(self.report)
        current["provenance"]["manifest"]["run_id"] = "other-run"
        diff = compare_runs(self.report, current)
        self.assertTrue(diff["compatible"])
        self.assertNotIn("run_id", diff["incompatibilities"])

    def test_application_journal_becomes_measurement_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            raw = self.bundle.model_dump()
            (root / "manifest.json").write_text(dumps(raw["manifest"]))
            (root / "scenarios.json").write_text(dumps(raw["scenarios"]))
            for key in ("turns", "snapshots", "events", "checks", "knowledge"):
                (root / f"{key}.json").write_text(dumps(raw[key]))
            request = raw["turns"][0]["request_id"]
            row = {"schema_version": "1.0.0", "event_id": "app-1", "event": "llm.completed",
                   "run_id": "synthetic-run", "request_id": request, "model": "synthetic-chat",
                   "input_tokens": 11, "output_tokens": 7, "provider_request_id": "prov-1",
                   "cache": "prompt", "hit": True, "call_id": "call-1", "status": "succeeded"}
            started = {**row, "event_id": "app-0", "event": "llm.started"}
            (root / "application-1.jsonl").write_text(json.dumps(started) + "\n" + json.dumps(row) + "\n")
            (root / "application-1.jsonl").write_bytes((root / "application-1.jsonl").read_bytes() + b"{bad")
            bundle = load_artifacts(root)
            self.assertTrue(any(d.kind == "application" and d.data.get("model") == "synthetic-chat"
                                for d in bundle.diagnostics))
            self.assertTrue(any(d.kind == "integrity" and d.source == "application-1.jsonl"
                                for d in bundle.diagnostics))
            measured = next(m for m in bundle.measurements if m.request_id == request)
            self.assertEqual(measured.input_tokens, 11)
            self.assertEqual(measured.output_tokens, 7)
            self.assertIsNone(measured.cost)

    def test_generated_checks_cover_stateful_turns_without_checks_json(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            raw = self.bundle.model_dump()
            (root / "manifest.json").write_text(dumps(raw["manifest"]))
            (root / "scenarios.json").write_text(dumps(raw["scenarios"]))
            for key in ("turns", "snapshots", "events", "knowledge"):
                (root / f"{key}.json").write_text(dumps(raw[key]))
            bundle = load_artifacts(root)
            self.assertTrue(bundle.checks)
            report = evaluate_run(bundle)
            self.assertTrue(any(r["assertion_id"].endswith(":state_coverage") for r in report["assertions"]))


if __name__ == "__main__":
    unittest.main()
