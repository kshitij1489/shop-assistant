"""Turn-level runner behavior."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from evaluate.contracts.models import AssertionResult, ReferenceTurn, StateSnapshot, TurnEvidence
from evaluate.evidence import read_journal
from evaluate.runner import (
    AwaitOptions, Components, EvaluationRunner, LoadOptions, ProjectionSettlementPolicy, RecoveryOptions,
    RunnerOptions, SnapshotBranchOracle, WebsiteTransport,
)
from evaluate.runner.defaults import normalize_question
from evaluate.runner.ports import Usage
from evaluate.tests.chat_server import ChatServer
from evaluate.tests.fakes import (
    FakeControls, FakeEvaluator, FakeInspector, FakeProvisioner, make_action, make_config, make_scenario,
)


class CrashingEvaluator(FakeEvaluator):
    """Raises on the first evaluate call, then behaves like FakeEvaluator."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def evaluate(self, scenario, turn, evidence: TurnEvidence, snapshots) -> list[AssertionResult]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("judge unavailable")
        return super().evaluate(scenario, turn, evidence, snapshots)


class InspectingProvisioner(FakeProvisioner):
    """Adds inspect() so cleanup can persist a provision manifest."""

    def __init__(self, server: ChatServer, **kwargs) -> None:
        super().__init__(server, **kwargs)
        self.inspect_calls: list[str] = []

    def inspect(self, lease):
        self.inspect_calls.append(lease.handle)
        return {"lease_id": lease.handle, "lifecycle": "active"}


class TurnSemanticsHarness(unittest.TestCase):
    def setUp(self):
        self.server = ChatServer().start()
        self.addCleanup(self.server.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "evidence"
        self.config = make_config(self.server)
        self.provisioner = InspectingProvisioner(self.server)
        self.controls = FakeControls()
        self.inspector = FakeInspector(self.server)

    def components(self, evaluator=FakeEvaluator(), **overrides) -> Components:
        transport = overrides.pop("transport", None)
        transport = transport or WebsiteTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        base = dict(provisioner=self.provisioner, controls=self.controls, transport=transport,
                    inspector=self.inspector, branch_oracle=SnapshotBranchOracle(),
                    settlement=ProjectionSettlementPolicy(), evaluator=evaluator)
        return Components(**{**base, **overrides})

    def run_scenarios(self, scenarios, options=None, components=None, **kwargs):
        runner = EvaluationRunner(
            self.config, scenarios, components or self.components(), self.directory,
            options or RunnerOptions(awaiting=AwaitOptions(max_wait_seconds=0.3, poll_interval_seconds=0.02)),
            **kwargs,
        )
        return runner.run()


class ResponseEvidenceBeforeSettlementTests(TurnSemanticsHarness):
    def test_response_evidence_stored_before_failing_inspector(self):
        """Finding 9: ledger completed must not leave the reply only in memory."""

        class AfterSnapshotFails(FakeInspector):
            def snapshot(self, lease, identity, original_turn_index, request_id, phase):
                if phase == "after":
                    raise RuntimeError("state store unavailable")
                return super().snapshot(lease, identity, original_turn_index, request_id, phase)

        self.inspector = AfterSnapshotFails(self.server)
        meter = Mock()
        meter.usage.return_value = Usage(tokens=120, cost_minor=3, currency="USD")
        outcome = self.run_scenarios(
            [make_scenario("inspect", ["hello", "again"])],
            components=self.components(usage=meter),
            options=RunnerOptions(
                recovery=RecoveryOptions(max_attempts=1),
                awaiting=AwaitOptions(enabled=False),
            ),
        )
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual(len(turns), 1)
        self.assertTrue(turns[0]["response_text"].startswith("echo:"))
        self.assertEqual(turns[0]["message"], "hello")
        self.assertEqual(outcome.attempts[0].execution_status, "ERROR")
        meter.usage.assert_called_once()
        self.assertEqual((outcome.budget.actual.requests, outcome.budget.actual.tokens,
                          outcome.budget.actual.cost_minor), (1, 120, 3))
        # Settlement/inspection failed, but the completed reply was already durable.
        self.assertEqual([d["status"] for d in read_journal(self.directory / "dispatch.jsonl").records
                          if d["status"] == "completed"], ["completed"])


class ExpectedPendingPaymentTests(TurnSemanticsHarness):
    def test_expected_pending_payment_does_not_block_when_later_capture_exists(self):
        """Finding 5: pending payment awaiting a later payment_control is not unsettled."""
        capture = make_action(
            "capture-later",
            {"kind": "payment_control", "operation": "capture", "amount_minor": 100},
            original_turn_index=1,
        )
        scenario = make_scenario("pay-then-capture", ["pay now", "after capture"], actions=[capture])
        # setup/before stay idle; after turn 0 is pending (expected); turn 1 settles.
        self.inspector.payment_sequence = ["none", "none", "pending", "captured"]
        outcome = self.run_scenarios([scenario])
        attempt = outcome.attempts[0]
        self.assertFalse(attempt.unsettled)
        self.assertNotEqual(attempt.outcome, "BLOCKED")
        self.assertEqual(attempt.execution_status, "COMPLETED")
        blocked = [e for e in read_journal(self.directory / "events.jsonl").records
                   if e.get("kind") == "snapshot" and e.get("status") == "blocked"]
        self.assertEqual(blocked, [])
        # Both turns still ran; capture action applied on the second turn.
        self.assertIn("capture-later", [a for _, a in self.controls.applied])
        self.assertEqual(attempt.turns_completed, 2)


class ExecutionEvaluationSplitTests(TurnSemanticsHarness):
    def test_unscored_completion_is_not_blocked(self):
        """Finding 12: finished conversation without an evaluator is COMPLETED, not BLOCKED.

        Design: ``execution_status=COMPLETED`` and ``evaluation_verdict=None`` are
        authoritative. Legacy ``outcome`` stays a value EvaluationSummary accepts
        (PASS) so runner.py does not crash; it is not a scored pass when
        evaluation_verdict is None.
        """
        outcome = self.run_scenarios(
            [make_scenario("plain", ["hello"])],
            components=self.components(evaluator=None),
        )
        attempt = outcome.attempts[0]
        self.assertEqual(attempt.execution_status, "COMPLETED")
        self.assertIsNone(attempt.evaluation_verdict)
        self.assertNotEqual(attempt.outcome, "BLOCKED")
        self.assertEqual(attempt.failure, "none")
        self.assertIn("no evaluator", attempt.detail)
        # Summary must still build with a legacy Outcome key.
        self.assertEqual(sum(outcome.summary.counts.values()), 1)
        self.assertEqual(outcome.summary.counts.get("BLOCKED", 0), 0)

    def test_evaluator_exception_does_not_skip_next_turn(self):
        """Finding 12: scoring failures are NEEDS_REVIEW and do not stop the script."""
        evaluator = CrashingEvaluator()
        outcome = self.run_scenarios(
            [make_scenario("score", ["first", "second"])],
            components=self.components(evaluator=evaluator),
        )
        attempt = outcome.attempts[0]
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual(len(turns), 2)
        self.assertEqual([t["message"] for t in turns], ["first", "second"])
        self.assertEqual(attempt.turns_completed, 2)
        self.assertEqual(attempt.execution_status, "COMPLETED")
        self.assertEqual(attempt.evaluation_verdict, "NEEDS_REVIEW")
        self.assertNotEqual(attempt.outcome, "ERROR")
        self.assertEqual(evaluator.calls, 2)
        self.assertEqual(list(self.server.state.sessions.values())[0].messages, ["first", "second"])


class UnicodeBranchOracleTests(unittest.TestCase):
    def test_non_latin_questions_still_match(self):
        """Finding 15: Unicode letters survive normalize_question."""
        hindi = "कितने पिस्ता आइसक्रीम?"
        self.assertEqual(normalize_question(hindi), normalize_question("कितने पिस्ता आइसक्रीम"))
        self.assertEqual(normalize_question("How many?"), normalize_question("how many"))
        self.assertNotEqual(normalize_question(hindi), normalize_question("कितने मैंगो"))
        snapshot = StateSnapshot(
            run_id="test-run", scenario_id="sessions:x", scenario_instance_id="inst", attempt=1,
            event_id="evt-1", snapshot_id="snap-1", original_turn_index=0, request_id=None,
            phase="before", captured_at="2026-09-29T08:30:00+00:00",
            state={"chat": {"pending_question": hindi}}, unavailable_sections=[],
        )
        reference = ReferenceTurn(original_turn_index=1, text="reference", asks="कितने पिस्ता आइसक्रीम?")
        self.assertEqual(SnapshotBranchOracle().pending_question(snapshot, reference, None), "matched")
        other = ReferenceTurn(original_turn_index=1, text="reference", asks="कुछ और पूछें")
        self.assertEqual(SnapshotBranchOracle().pending_question(snapshot, other, None), "unknown")


class WarmUpEvidenceTests(TurnSemanticsHarness):
    def test_warm_up_copied_onto_turn_evidence(self):
        """Finding 23 support: TurnEvidence.warm_up mirrors the dispatch warm_up flag."""
        outcome = self.run_scenarios(
            [make_scenario("warm", ["hello"]), make_scenario("measured", ["hi"])],
            options=RunnerOptions(
                awaiting=AwaitOptions(enabled=False),
                load=LoadOptions(concurrency=1, warm_up_sessions=1),
            ),
        )
        turns = read_journal(self.directory / "turns.jsonl").records
        by_scenario = {t["scenario_id"]: t for t in turns}
        self.assertTrue(by_scenario["sessions:warm"]["warm_up"])
        self.assertFalse(by_scenario["sessions:measured"]["warm_up"])
        self.assertEqual(outcome.load.warm_up.sessions_completed, 1)


class ProvisionInspectCleanupTests(TurnSemanticsHarness):
    def test_cleanup_persists_provision_inspect_manifest(self):
        """Finding 17 runner half: inspect(lease) written before release."""
        outcome = self.run_scenarios(
            [make_scenario("prov", ["hello"])],
            options=RunnerOptions(awaiting=AwaitOptions(enabled=False)),
        )
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        self.assertEqual(len(self.provisioner.inspect_calls), 2)
        handle = self.provisioner.inspect_calls[0]
        path = self.directory / f"provision-{handle}.json"
        self.assertTrue(path.exists())
        manifest = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["lease_id"], handle)
        self.assertEqual(self.provisioner.cleaned, [handle])



class DispatchProgressRegressionTests(TurnSemanticsHarness):
    def test_before_snapshot_includes_same_turn_actions(self):
        controls = self.controls

        class ActionInspector(FakeInspector):
            def snapshot(self, lease, identity, original_turn_index, request_id, phase):
                snapshot = super().snapshot(lease, identity, original_turn_index, request_id, phase)
                snapshot.state['applied_actions'] = [action_id for _, action_id in controls.applied]
                return snapshot

        action = make_action('capture', {'kind': 'payment_control', 'operation': 'capture',
                                        'amount_minor': 100}, original_turn_index=0)
        self.inspector = ActionInspector(self.server)
        outcome = self.run_scenarios([make_scenario('actions', ['after capture'], actions=[action])])
        self.assertEqual(outcome.attempts[0].turns_completed, 1)
        snapshots = read_journal(self.directory / 'snapshots.jsonl').records
        before = next(s for s in snapshots if s['phase'] == 'before' and s['original_turn_index'] == 0)
        self.assertEqual(before['state']['applied_actions'], ['capture'])
        turn = read_journal(self.directory / 'turns.jsonl').records[0]
        self.assertEqual(turn['snapshot_ids'], [before['snapshot_id']])

    def test_provider_failure_preserves_successful_reply_without_replay(self):
        from evaluate.contracts.interfaces import Blocked
        base = WebsiteTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        evidence_dir = self.directory
        meter = Mock()
        meter.usage.return_value = Usage(tokens=120, cost_minor=3, currency='USD')

        class ProgressTransport:
            def __getattr__(self, name):
                return getattr(base, name)

            def advance(self, lease):
                replies = read_journal(evidence_dir / 'turns.jsonl').records
                assert replies[0]['response_text'] == 'echo:first'
                ledger = read_journal(evidence_dir / 'dispatch.jsonl').records
                assert ledger[-1]['status'] == 'completed'
                meter.usage.assert_called_once()
                raise Blocked('provider unavailable after response')

        outcome = self.run_scenarios([make_scenario('provider-failure', ['first', 'second'])],
            components=self.components(transport=ProgressTransport(), usage=meter))
        self.assertEqual(len(outcome.attempts), 1)
        self.assertEqual(outcome.attempts[0].completed_requests, 1)
        self.assertEqual(outcome.attempts[0].execution_status, 'ERROR')
        self.assertEqual((outcome.budget.actual.requests, outcome.budget.actual.tokens,
                          outcome.budget.actual.cost_minor), (1, 120, 3))
        self.assertEqual(len(read_journal(self.directory / 'turns.jsonl').records), 1)
        self.assertEqual([d['status'] for d in read_journal(self.directory / 'dispatch.jsonl').records],
                         ['intent', 'completed'])

    def test_settlement_interruption_records_every_unattempted_turn(self):
        from evaluate.runner.awaiting import await_settlement

        def interrupt(context, *args, **kwargs):
            context.stop_event.set()
            return await_settlement(context, *args, **kwargs)

        self.inspector.payment_sequence = ['pending']
        capture = make_action('capture', {'kind': 'payment_control', 'operation': 'capture',
                                         'amount_minor': 100}, original_turn_index=0)
        scenario = make_scenario('settlement-interrupt', ['first', 'second', 'third'], actions=[capture])
        with patch('evaluate.runner.turns.await_settlement', side_effect=interrupt):
            outcome = self.run_scenarios([scenario])
        self.assertEqual(outcome.attempts[0].execution_status, 'INTERRUPTED')
        events = read_journal(self.directory / 'events.jsonl').records
        abandoned = [e for e in events if e['detail'].startswith('turn not attempted:')]
        self.assertEqual([e['original_turn_index'] for e in abandoned], [1, 2])
        self.assertTrue(all(e['status'] == 'blocked' and 'settlement' in e['detail'] for e in abandoned))
        self.assertEqual(len(read_journal(self.directory / 'turns.jsonl').records), 1)

    def test_graceful_interruption_resumes_in_a_fresh_attempt(self):
        from unittest.mock import patch
        from evaluate.runner.scenario import ScenarioExecutor
        original = ScenarioExecutor._absorb

        def interrupt_after_first(executor, result, outcome):
            original(executor, result, outcome)
            executor.context.stop_event.set()

        scenario = make_scenario('interrupted', ['first', 'second'])
        with patch.object(ScenarioExecutor, '_absorb', interrupt_after_first):
            interrupted = self.run_scenarios([scenario])
        self.assertEqual(interrupted.attempts[0].execution_status, 'INTERRUPTED')
        self.assertFalse((self.directory / 'summary.json').exists())
        record = read_journal(self.directory / 'attempts.jsonl').records[0]
        self.assertTrue(record['restart_pending'])
        self.assertEqual(record['execution_status'], 'INTERRUPTED')
        resumed = self.run_scenarios([scenario], resume=True)
        self.assertEqual([r.identity.attempt for r in resumed.attempts], [1, 2])
        self.assertEqual(resumed.attempts[0].execution_status, 'INTERRUPTED')
        self.assertEqual(resumed.attempts[-1].turns_completed, 2)
        self.assertEqual(len(read_journal(self.directory / 'turns.jsonl').records), 3)

    def test_provision_manifest_is_present_before_first_chat(self):
        base = WebsiteTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        evidence_dir = self.directory

        class CheckingTransport:
            def __getattr__(self, name):
                return getattr(base, name)

            def send(self, lease, request):
                assert (evidence_dir / f'provision-{lease.handle}.json').is_file()
                return base.send(lease, request)

        result = self.run_scenarios([make_scenario('manifest', ['hello'])],
                                   components=self.components(transport=CheckingTransport()))
        self.assertEqual(result.attempts[0].turns_completed, 1)


if __name__ == "__main__":
    unittest.main()
