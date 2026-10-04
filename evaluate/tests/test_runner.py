import json
from pathlib import Path
import tempfile
import unittest
import urllib.error

from evaluate.contracts.interfaces import ChatRequest
from evaluate.contracts.models import ExecutionIdentity, ReferenceTurn, StateSnapshot
from evaluate.evidence import EvidenceConflict, read_journal
from evaluate.identity import instance_id
from evaluate.runner import (
    AwaitOptions, BranchOptions, BudgetOptions, Components, EvaluationRunner, LoadOptions,
    ProjectionSettlementPolicy, RecoveryOptions, RunnerOptions, SnapshotBranchOracle, Usage, WebsiteChatResponse,
    WebsiteCredential, WebsiteTransport, classify_failure,
)
from evaluate.tests.chat_server import ChatServer
from evaluate.tests.fakes import (
    API_KEY, FakeControls, FakeEvaluator, FakeInspector, FakeProvisioner, make_action, make_config, make_scenario,
    make_turn,
)


class SimulatedCrash(BaseException):
    """Escapes every `except Exception` like a real process death would."""


class CrashingTransport(WebsiteTransport):
    def __init__(self, *args, crash_on: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.crash_on = crash_on

    def send(self, lease, request):
        if request.turn.text == self.crash_on:
            raise SimulatedCrash(request.turn.text)
        return super().send(lease, request)


class RunnerHarness(unittest.TestCase):
    def setUp(self):
        self.server = ChatServer().start()
        self.addCleanup(self.server.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "evidence"
        self.config = make_config(self.server)
        self.provisioner = FakeProvisioner(self.server)
        self.controls = FakeControls()
        self.inspector = FakeInspector(self.server)

    def components(self, evaluator=FakeEvaluator(), transport=None, **overrides) -> Components:
        transport = transport or WebsiteTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        base = dict(provisioner=self.provisioner, controls=self.controls, transport=transport, inspector=self.inspector,
                    branch_oracle=SnapshotBranchOracle(), settlement=ProjectionSettlementPolicy(), evaluator=evaluator)
        return Components(**{**base, **overrides})

    def run_scenarios(self, scenarios, options=None, components=None, resume=False, directory=None, **kwargs):
        runner = EvaluationRunner(self.config, scenarios, components or self.components(), directory or self.directory,
                                  options or RunnerOptions(awaiting=AwaitOptions(max_wait_seconds=0.3, poll_interval_seconds=0.02)),
                                  resume=resume, **kwargs)
        return runner.run()

    def events(self, kind=None):
        records = read_journal(self.directory / "events.jsonl").records
        return [r for r in records if kind is None or r["kind"] == kind]

    def dispatches(self):
        return read_journal(self.directory / "dispatch.jsonl").records

    def assert_no_secrets_on_disk(self):
        tokens = list(self.server.state.tokens)
        cookies = list(self.server.state.sessions)
        for path in self.directory.rglob("*"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                self.assertNotIn(API_KEY, text, path.name)
                for secret in tokens + cookies:
                    self.assertNotIn(secret, text, path.name)


class SequentialRunTests(RunnerHarness):
    def test_sessions_are_isolated_and_evidence_is_complete(self):
        scenarios = [make_scenario("a", ["hello", "menu please"]), make_scenario("b", ["hi", "price of pistachio"])]
        outcome = self.run_scenarios(scenarios)
        self.assertEqual(outcome.summary.counts["PASS"], 2)
        sessions = self.server.state.sessions
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sorted(tuple(s.messages) for s in sessions.values()),
                         [("hello", "menu please"), ("hi", "price of pistachio")])
        self.assertEqual(self.server.state.token_requests, 2)
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual(len(turns), 4)
        # Finding 9: reply is journaled before after-snapshot; TurnEvidence is not rewritten.
        self.assertTrue(all(t["http_status"] == 200 and t["response_text"].startswith("echo:") for t in turns))
        self.assertTrue(all(len(t["snapshot_ids"]) == 1 for t in turns))
        snapshots = read_journal(self.directory / "snapshots.jsonl").records
        self.assertGreaterEqual(len(snapshots), 8)  # before + after per turn; not folded into turns.jsonl
        statuses = [(d["request_id"], d["status"]) for d in self.dispatches()]
        self.assertEqual([s for _, s in statuses], ["intent", "completed"] * 4)
        self.assertEqual(self.dispatches()[0]["expectation"]["expected_facts"], ["fact for hello"])
        kinds = [e["kind"] for e in self.events()]
        self.assertLess(kinds.index("request"), kinds.index("response"))
        self.assertEqual(len(self.events("cleanup")), 4)
        self.assertEqual(len(self.provisioner.cleaned), 2)
        self.assertEqual(len(read_journal(self.directory / "assertions.jsonl").records), 4)
        self.assertTrue((self.directory / "summary.json").exists())
        self.assertTrue((self.directory / "reports" / "load.json").exists())
        self.assert_no_secrets_on_disk()

    def test_http_errors_and_blank_probe_are_completed_requests(self):
        outcome = self.run_scenarios([make_scenario("errors", ["fine", "[status-500] boom", "   ", "after"])])
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual([t["http_status"] for t in turns], [200, 500, 400, 200])
        self.assertEqual(turns[2]["message"], "   ")  # sent unchanged so HTTP 400 is observable
        self.assertEqual(self.server.state.sessions and list(self.server.state.sessions.values())[0].messages,
                         ["fine", "[status-500] boom", "after"])
        self.assertEqual(outcome.summary.counts["FAIL"], 1)
        self.assertEqual([d["status"] for d in self.dispatches()][1::2], ["completed"] * 4)

    def test_without_evaluator_execution_is_completed_unscored(self):
        # Finding 12: finished run without an evaluator is COMPLETED, not BLOCKED.
        # Completed execution has its own outcome; only scoring can produce PASS.
        outcome = self.run_scenarios([make_scenario("plain", ["hello"])], components=self.components(evaluator=None))
        attempt = outcome.attempts[0]
        self.assertEqual(attempt.execution_status, "COMPLETED")
        self.assertIsNone(attempt.evaluation_verdict)
        self.assertEqual(attempt.outcome, "COMPLETED")
        self.assertEqual(outcome.summary.counts["PASS"], 0)
        self.assertEqual(outcome.summary.counts["COMPLETED"], 1)
        self.assertEqual(outcome.summary.counts.get("BLOCKED", 0), 0)
        self.assertIn("no evaluator", attempt.detail)

    def test_blocked_scenarios_never_provision(self):
        outcome = self.run_scenarios([make_scenario("blocked", ["hello"], blockers=True)])
        self.assertEqual(outcome.summary.counts["BLOCKED"], 1)
        self.assertEqual(self.server.state.token_requests, 0)
        self.assertEqual(self.provisioner.provisioned, [])
        self.assertEqual(self.events("provision")[0]["status"], "blocked")

    def test_actions_run_at_original_indexes_in_order(self):
        actions = [make_action("pre", {"kind": "freeze_clock", "clock": {"at": "2026-09-29T14:00:00+05:30"}}),
                   make_action("turn2-a", {"kind": "catalog_control", "item_name": "Pistachio", "variant_name": "QA standard", "price_minor": 47000}, 2),
                   make_action("turn2-b", {"kind": "catalog_control", "item_name": "Pistachio", "variant_name": "QA standard", "available": False}, 2)]
        scenario = make_scenario("acts", ["one", "two", "three"], actions=actions)
        self.run_scenarios([scenario])
        self.assertEqual([a for _, a in self.controls.applied], ["pre", "turn2-a", "turn2-b"])
        event_order = [(e["kind"], e.get("action_id"), e.get("original_turn_index")) for e in self.events() if e["kind"] in ("action", "request")]
        self.assertEqual(event_order[:2], [("action", "pre", None), ("request", None, 0)])
        self.assertEqual(event_order[2:5], [("request", None, 1), ("action", "turn2-a", 2), ("action", "turn2-b", 2)])


class RecoveryTests(RunnerHarness):
    def test_timeout_is_ambiguous_never_replayed_and_restarted_with_fresh_identity(self):
        scenario = make_scenario("ambiguous", ["hello", "[hang-once] pay now", "done"])
        outcome = self.run_scenarios([scenario])
        results = sorted(outcome.summary.results, key=lambda r: r.attempt)
        self.assertEqual([r.outcome for r in results], ["ERROR", "PASS"])
        self.assertEqual(results[0].scenario_instance_id, results[1].scenario_instance_id)
        by_session = [s.messages for s in self.server.state.sessions.values()]
        self.assertEqual(sorted(by_session), [["hello", "[hang-once] pay now"], ["hello", "[hang-once] pay now", "done"]])
        ledger = [(d["attempt"], d["user_turn_index"], d["status"]) for d in self.dispatches()]
        self.assertIn((1, 1, "in_flight_unknown"), ledger)
        self.assertEqual(sum(1 for a, t, s in ledger if a == 1 and t == 1 and s == "intent"), 1)
        turn = next(t for t in read_journal(self.directory / "turns.jsonl").records if t["attempt"] == 1 and t["user_turn_index"] == 1)
        self.assertEqual(turn["transport_error"], "timeout")
        # Finding 9: journaled TurnEvidence keeps early (before) snapshot ids only.
        self.assertEqual(len(turn["snapshot_ids"]), 1)
        after_snaps = [s for s in read_journal(self.directory / "snapshots.jsonl").records
                       if s["attempt"] == 1 and s["original_turn_index"] == turn["original_turn_index"]
                       and s["phase"] == "after"]
        self.assertGreaterEqual(len(after_snaps), 1)  # after-inspect is a separate StateSnapshot
        blocked = [e for e in self.events("error") if e["status"] == "blocked" and e["attempt"] == 1]
        self.assertEqual([e["user_turn_index"] for e in blocked], [2])
        self.assertEqual(len(self.provisioner.cleaned), 2)

    def test_not_dispatched_failures_get_exactly_one_fresh_request(self):
        class RefusingOnce(WebsiteTransport):
            refused = False
            def send(self, lease, request):
                if request.turn.text == "flaky" and not self.refused:
                    self.refused = True
                    exc = urllib.error.URLError(ConnectionRefusedError(61, "refused"))
                    state, label = classify_failure(exc)
                    return WebsiteChatResponse(None, None, 1.0, label, dispatch_state=state)
                return super().send(lease, request)
        transport = RefusingOnce(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        outcome = self.run_scenarios([make_scenario("refuse", ["flaky", "next"])], components=self.components(transport=transport))
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        statuses = [d["status"] for d in self.dispatches() if d["user_turn_index"] == 0]
        self.assertEqual(statuses, ["intent", "not_dispatched", "intent", "completed"])
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual(len([t for t in turns if t["user_turn_index"] == 0]), 2)  # failed attempt retained
        self.assertEqual(list(self.server.state.sessions.values())[0].messages, ["flaky", "next"])

    def test_interrupted_run_is_marked_and_resumed_with_new_attempt(self):
        scenario = make_scenario("interrupt", ["hello", "crash-here", "bye"])
        transport = CrashingTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds, crash_on="crash-here")
        with self.assertRaises(SimulatedCrash):
            self.run_scenarios([scenario], components=self.components(transport=transport))
        self.assertFalse((self.directory / "summary.json").exists())
        events_before = len(self.events())
        self.assertEqual(self.dispatches()[-1]["status"], "intent")
        with self.assertRaises(EvidenceConflict):
            self.run_scenarios([scenario])  # never overwrite without resume
        outcome = self.run_scenarios([scenario], resume=True)
        self.assertEqual(len(outcome.recovery.interrupted), 1)
        interrupted = [d for d in self.dispatches() if d["status"] == "interrupted"]
        self.assertEqual(len(interrupted), 1)
        self.assertEqual(interrupted[0]["request_id"], outcome.recovery.interrupted[0].request_id)
        self.assertEqual([r.attempt for r in outcome.summary.results], [2])
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        self.assertGreater(len(self.events()), events_before)
        self.assertEqual(len([e for e in self.events() if e["attempt"] == 1]), events_before)

    def test_crashing_control_records_redacted_crash_and_restarts(self):
        self.controls = FakeControls(raise_ids={"boom"})
        scenario = make_scenario("crash", ["hello"], actions=[make_action("boom", {"kind": "freeze_clock", "clock": {"at": "2026-09-29T14:00:00+05:30"}})])
        outcome = self.run_scenarios([scenario], options=RunnerOptions(recovery=RecoveryOptions(max_attempts=2)))
        self.assertEqual([r.outcome for r in outcome.summary.results], ["ERROR", "ERROR"])
        self.assertEqual(len(self.provisioner.provisioned), 2)
        crashes = sorted((self.directory / "crashes").glob("*.json"))
        self.assertEqual(len(crashes), 2)
        record = json.loads(crashes[0].read_text())
        self.assertEqual(record["phase"], "setup_action")
        self.assertEqual(record["exception_type"], "builtins.RuntimeError")
        self.assertEqual(record["scenario_id"], scenario.scenario_id)
        self.assertNotIn("eyJabcdefghijk", json.dumps(record))
        self.assertTrue(any("setup_action" in e["detail"] for e in self.events("error")))
        self.assertEqual(len(self.provisioner.cleaned), 2)

    def test_completed_request_is_not_replayed_when_later_scoring_or_inspection_fails(self):
        class AfterSnapshotFails(FakeInspector):
            def snapshot(self, lease, identity, original_turn_index, request_id, phase):
                if phase == "after":
                    raise RuntimeError("state store unavailable")
                return super().snapshot(lease, identity, original_turn_index, request_id, phase)
        self.inspector = AfterSnapshotFails(self.server)
        outcome = self.run_scenarios([make_scenario("inspect", ["hello", "again"])],
                                     options=RunnerOptions(recovery=RecoveryOptions(max_attempts=2), awaiting=AwaitOptions(enabled=False)))
        self.assertEqual([r.outcome for r in outcome.summary.results], ["ERROR"])
        self.assertEqual([s.messages for s in self.server.state.sessions.values()], [["hello"]])

    def test_known_failure_is_not_restarted_after_a_later_timeout(self):
        outcome = self.run_scenarios([make_scenario("fail-then-hang", ["[fail] no", "[hang-once] later"])],
                                     options=RunnerOptions(recovery=RecoveryOptions(max_attempts=2)))
        self.assertEqual([r.outcome for r in outcome.summary.results], ["FAIL"])
        self.assertEqual(len(self.server.state.sessions), 1)

    def test_resume_summary_includes_attempts_that_already_finished(self):
        done = make_scenario("done", ["hello"])
        interrupted = make_scenario("interrupt", ["crash-here"])
        transport = CrashingTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds, crash_on="crash-here")
        with self.assertRaises(SimulatedCrash):
            self.run_scenarios([done, interrupted], components=self.components(transport=transport))
        outcome = self.run_scenarios([done, interrupted], components=self.components(), resume=True)
        finished = {(r.scenario_id, r.attempt, r.outcome) for r in outcome.summary.results}
        self.assertIn(("sessions:done", 1, "PASS"), finished)
        self.assertIn(("sessions:interrupt", 2, "PASS"), finished)

    def test_blocked_provisioning_is_blocked_not_restarted(self):
        self.provisioner = FakeProvisioner(self.server, block=True)
        outcome = self.run_scenarios([make_scenario("nofix", ["hello"])])
        self.assertEqual([r.outcome for r in outcome.summary.results], ["BLOCKED"])
        self.assertEqual(self.server.state.token_requests, 0)


class BranchTests(RunnerHarness):
    def scenario(self):
        return make_scenario("branch", ["add pistachio", "two please", "checkout"],
                             references={1: "How many Pistachio Ice Cream?"}, answers={1: 1})

    def test_matching_pending_question_proceeds(self):
        self.inspector.pending_question = "how many pistachio ice cream"
        outcome = self.run_scenarios([self.scenario()])
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual([t["branch"] for t in turns], ["not_applicable", "matched", "not_applicable"])

    def test_mismatch_blocks_dependent_turns(self):
        self.inspector.pending_question = None
        outcome = self.run_scenarios([self.scenario()])
        self.assertEqual(outcome.summary.counts["BLOCKED"], 1)
        self.assertEqual(list(self.server.state.sessions.values())[0].messages, ["add pistachio"])
        mismatch = self.events("branch_mismatch")
        self.assertEqual((mismatch[0]["status"], mismatch[0]["user_turn_index"]), ("blocked", 1))
        blocked = [e["user_turn_index"] for e in self.events("error") if e["status"] == "blocked"]
        self.assertEqual(blocked, [2])

    def test_unavailable_pending_state_blocks_instead_of_assuming(self):
        self.inspector.chat_unavailable = True
        outcome = self.run_scenarios([self.scenario()])
        self.assertEqual(outcome.summary.counts["BLOCKED"], 1)
        self.assertIn("unavailable", self.events("branch_mismatch")[0]["detail"])

    def test_continue_flagged_policy_sends_and_marks_mismatch(self):
        self.inspector.pending_question = None
        options = RunnerOptions(branching=BranchOptions(on_mismatch="continue_flagged"))
        outcome = self.run_scenarios([self.scenario()], options=options)
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual(turns[1]["branch"], "mismatch")

    def test_continuation_runs_as_separate_instance_with_fixture(self):
        fixture = make_action("pending-qty", {"kind": "seed_fixture", "fixture_id": "pending-quantity-pistachio", "fixture_hash": "b" * 64})
        inspector = self.inspector
        class Planner:
            def plan(self, scenario, turn, reference):
                return fixture.model_copy(update={"scenario_id": scenario.scenario_id})
        class SeedingControls(FakeControls):
            def apply(self, lease, identity, action):
                if action.action_id == "pending-qty":
                    inspector.pending_question = "How many Pistachio Ice Cream?"  # the fixture establishes the state
                return super().apply(lease, identity, action)
        self.controls = SeedingControls()
        inspector.pending_question = None
        components = self.components(continuation_planner=Planner())
        outcome = self.run_scenarios([self.scenario()], components=components,
                                     fixture_hashes={"pending-quantity-pistachio": "b" * 64})
        results = outcome.summary.results
        self.assertEqual([r.outcome for r in results], ["BLOCKED", "PASS"])
        self.assertNotEqual(results[0].scenario_instance_id, results[1].scenario_instance_id)
        self.assertEqual(results[0].scenario_instance_id, instance_id(self.config.run_id, self.scenario().scenario_id, 0))
        self.assertIn("pending-qty", [action for _, action in self.controls.applied])
        continuation_messages = sorted(s.messages for s in self.server.state.sessions.values())
        self.assertEqual(continuation_messages, [["add pistachio"], ["two please", "checkout"]])
        provision = [e for e in self.events("provision") if "continuation" in e["detail"]]
        self.assertEqual(provision[0]["scenario_instance_id"], results[1].scenario_instance_id)
        turns = read_journal(self.directory / "turns.jsonl").records
        self.assertEqual([t["branch"] for t in turns if t["scenario_instance_id"] == results[1].scenario_instance_id],
                         ["matched", "not_applicable"])

    def test_continuation_that_diverges_again_does_not_spawn_another(self):
        fixture = make_action("noop-fixture", {"kind": "seed_fixture", "fixture_id": "noop", "fixture_hash": "d" * 64})
        class Planner:
            def plan(self, scenario, turn, reference):
                return fixture.model_copy(update={"scenario_id": scenario.scenario_id})
        self.inspector.pending_question = None
        outcome = self.run_scenarios([self.scenario()], components=self.components(continuation_planner=Planner()),
                                     fixture_hashes={"noop": "d" * 64})
        self.assertEqual([r.outcome for r in outcome.summary.results], ["BLOCKED", "BLOCKED"])
        self.assertEqual(len(self.provisioner.provisioned), 2)

    def test_unregistered_continuation_fixture_is_rejected(self):
        fixture = make_action("bad", {"kind": "seed_fixture", "fixture_id": "unknown", "fixture_hash": "c" * 64})
        class Planner:
            def plan(self, scenario, turn, reference):
                return fixture.model_copy(update={"scenario_id": scenario.scenario_id})
        self.inspector.pending_question = None
        outcome = self.run_scenarios([self.scenario()], components=self.components(continuation_planner=Planner()))
        self.assertEqual(len(outcome.summary.results), 1)
        self.assertTrue(any("continuation rejected" in e["detail"] for e in self.events("branch_mismatch")))

    def test_mismatch_does_not_apply_the_turn_action(self):
        action = make_action("mutate", {"kind": "catalog_control", "item_name": "Pistachio", "variant_name": "QA standard", "available": False}, 2)
        self.inspector.pending_question = None
        self.run_scenarios([make_scenario("guard", ["add pistachio", "two please"], references={1: "How many Pistachio Ice Cream?"},
                                          answers={1: 1}, actions=[action])])
        self.assertNotIn("mutate", [applied for _, applied in self.controls.applied])


class SettlementTests(RunnerHarness):
    def test_transitions_are_recorded_until_settled(self):
        self.inspector.payment_sequence = ["none", "none", "pending", "pending", "captured"]
        self.run_scenarios([make_scenario("pay", ["pay"], actions=[make_action("capture", {"kind": "payment_control", "operation": "capture", "amount_minor": 100}, 0)])])
        details = [e["detail"] for e in self.events("snapshot")]
        self.assertTrue(any(d.startswith("awaiting settlement of payment") for d in details))
        self.assertTrue(any(d.startswith("transition observed in payment") for d in details))
        self.assertTrue(any(d.startswith("settled after") for d in details))
        # Finding 9: turn journal keeps early persist ids; settlement snapshots are separate artifacts.
        turn = read_journal(self.directory / "turns.jsonl").records[0]
        self.assertEqual(len(turn["snapshot_ids"]), 1)
        self.assertTrue(turn["response_text"])
        snapshots = read_journal(self.directory / "snapshots.jsonl").records
        self.assertGreaterEqual(len(snapshots), 4)

    def test_timeout_reason_is_recorded(self):
        self.inspector.payment_sequence = ["none", "none", "pending"]
        outcome = self.run_scenarios([make_scenario("stuck", ["pay"], actions=[make_action("capture", {"kind": "payment_control", "operation": "capture", "amount_minor": 100}, 0)])])
        blocked = [e for e in self.events("snapshot") if e["status"] == "blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertIn("timeout", blocked[0]["detail"])
        self.assertIn("still pending: payment", blocked[0]["detail"])
        self.assertEqual(outcome.summary.counts["BLOCKED"], 1)
        self.assertIn("did not settle", outcome.attempts[0].detail)


class TransportTests(RunnerHarness):
    def test_wrong_api_key_blocks_before_any_message(self):
        class WrongKey(FakeProvisioner):
            def resolve(self, lease):
                return WebsiteCredential("qa-tenant", "not-the-key")
        self.provisioner = WrongKey(self.server)
        outcome = self.run_scenarios([make_scenario("auth", ["hello"])])
        self.assertEqual(outcome.summary.counts["BLOCKED"], 1)
        self.assertTrue(any("HTTP 403" in e["detail"] for e in self.events("provision")))
        self.assertEqual(self.server.state.sessions, {})
        self.assert_no_secrets_on_disk()

    def test_disconnect_is_ambiguous_and_transport_never_exposes_state(self):
        transport = WebsiteTransport(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        identity = ExecutionIdentity(run_id="test-run", scenario_id="sessions:x", scenario_instance_id="inst", attempt=1)
        lease = self.provisioner.provision(self.config, None, identity)
        response = transport.send(lease, ChatRequest(identity, "req-1", make_turn(0, 0, "[disconnect] gone")))
        self.assertEqual((response.dispatch_state, response.transport_error), ("in_flight_unknown", "connection"))
        self.assertNotIn(API_KEY, repr(transport) + repr(transport._sessions))
        transport.close(lease)
        self.assertEqual(transport._sessions, {})


class ConcurrencyTests(RunnerHarness):
    def test_bounded_concurrency_ramp_warmup_exclusivity_and_limits(self):
        exclusive = make_scenario("exclusive", ["[slow:0.15] frozen", "[slow:0.15] still frozen"],
                                  actions=[make_action("freeze", {"kind": "freeze_clock", "clock": {"at": "2026-09-29T14:00:00+05:30"}})])
        shared = [make_scenario(f"shared{i}", [f"[slow:0.1] one {i}", f"[slow:0.1] two {i}"]) for i in range(5)]
        scenarios = [shared[0], shared[1], exclusive, *shared[2:]]
        options = RunnerOptions(awaiting=AwaitOptions(enabled=False),
                                load=LoadOptions(concurrency=3, ramp_up_seconds=0.2, pacing_seconds=0.02, warm_up_sessions=2))
        outcome = self.run_scenarios(scenarios, options=options)
        self.assertEqual(outcome.summary.counts["PASS"], 6)
        load = outcome.load
        self.assertLessEqual(load.achieved_concurrency_max, 3)
        self.assertGreaterEqual(load.achieved_concurrency_max, 2)
        self.assertEqual((load.warm_up.sessions_completed, load.measured.sessions_completed), (2, 4))
        self.assertEqual(load.measured.requests_sent, 8)
        self.assertEqual(load.stop_reason, "completed")
        self.assertIsNotNone(load.measured.latency.p95_ms)
        spans = self.server.state.spans
        exclusive_spans = [s for s in spans if "frozen" in s.message]
        others = [s for s in spans if "frozen" not in s.message]
        for span in exclusive_spans:
            overlapping = [o for o in others if o.started < span.finished and o.finished > span.started]
            self.assertEqual(overlapping, [], "exclusive scenario overlapped a shared session")
        self.assertEqual(sum(d["warm_up"] for d in self.dispatches() if d["status"] == "completed"), 4)
        self.assert_no_secrets_on_disk()

    def test_request_limit_and_budget_stop_launching_and_withhold_dispatch(self):
        scenarios = [make_scenario(f"s{i}", ["a", "b"]) for i in range(4)]
        options = RunnerOptions(awaiting=AwaitOptions(enabled=False), load=LoadOptions(concurrency=1, max_requests=3))
        outcome = self.run_scenarios(scenarios, options=options)
        self.assertEqual(len([d for d in self.dispatches() if d["status"] == "completed"]), 3)
        self.assertEqual(outcome.load.stop_reason, "request_limit")
        self.assertTrue(outcome.budget.exhausted)
        self.assertEqual(outcome.budget.exhausted_reason, "request limit reached")
        self.assertEqual(outcome.budget.estimated.requests, 3)
        self.assertEqual(outcome.budget.actual.source, "unavailable")
        self.assertIsNone(outcome.budget.actual.tokens)
        withheld = [e for e in self.events("error") if "dispatch withheld" in e["detail"]]
        self.assertEqual(len(withheld), 1)
        self.assertEqual(outcome.summary.counts["SKIPPED"], 3)
        self.assertEqual(outcome.summary.counts["PASS"], 1)

    def test_duration_and_session_limits_stop_launching(self):
        scenarios = [make_scenario(f"d{i}", ["[slow:0.1] a"]) for i in range(6)]
        options = RunnerOptions(awaiting=AwaitOptions(enabled=False), load=LoadOptions(concurrency=1, duration_seconds=0.15))
        outcome = self.run_scenarios(scenarios, options=options)
        self.assertEqual(outcome.load.stop_reason, "duration_elapsed")
        executed = outcome.summary.counts["PASS"]
        self.assertLess(executed, 6)
        self.assertEqual(executed + outcome.summary.counts["SKIPPED"], 6)
        options = RunnerOptions(awaiting=AwaitOptions(enabled=False), load=LoadOptions(concurrency=2, max_sessions=3))
        outcome = self.run_scenarios(scenarios, options=options, directory=self.directory.with_name("second"))
        self.assertEqual(outcome.load.stop_reason, "session_limit")
        self.assertEqual((outcome.summary.counts["PASS"], outcome.summary.counts["SKIPPED"]), (3, 3))

    def test_reply_resembling_a_secret_is_masked_not_fatal(self):
        outcome = self.run_scenarios([make_scenario("mask", ["say Bearer abcdefghijklmnopqrstuvwxyz0123"])])
        self.assertEqual(len(outcome.summary.results), 1)
        turn = read_journal(self.directory / "turns.jsonl").records[0]
        self.assertIn("[REDACTED:bearer]", turn["response_text"])
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz0123", turn["response_text"])

    def test_evaluator_emitting_unsafe_artifacts_is_needs_review_not_error(self):
        # Finding 12: unsafe evaluator output is NEEDS_REVIEW; execution stays COMPLETED and later turns run.
        leaky = FakeEvaluator(criterion="Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123")
        outcome = self.run_scenarios([make_scenario("leak", ["hello", "still-runs"])],
                                     components=self.components(evaluator=leaky))
        attempt = outcome.attempts[0]
        self.assertEqual(attempt.execution_status, "COMPLETED")
        self.assertEqual(attempt.evaluation_verdict, "NEEDS_REVIEW")
        self.assertNotEqual(attempt.outcome, "ERROR")
        self.assertEqual(attempt.turns_completed, 2)
        self.assertEqual(list(self.server.state.sessions.values())[0].messages, ["hello", "still-runs"])
        self.assertEqual(len(self.provisioner.provisioned), 1)
        self.assertEqual(read_journal(self.directory / "assertions.jsonl").records, [])
        crash = json.loads(next((self.directory / "crashes").glob("*.json")).read_text())
        self.assertEqual((crash["phase"], crash["exception_type"]), ("evaluate", "evaluate.evidence.redaction.RedactionError"))

    def test_live_call_budget_and_actual_usage_are_reported_distinctly(self):
        class Meter:
            def usage(self, lease, request_id):
                return Usage(tokens=120, cost_minor=3, currency="USD")
        options = RunnerOptions(awaiting=AwaitOptions(enabled=False),
                                budget=BudgetOptions(max_live_calls=2, estimated_tokens_per_request=1000,
                                                     cost_per_million_tokens_minor=200000))
        outcome = self.run_scenarios([make_scenario("cost", ["a", "b", "c"])], options=options,
                                     components=self.components(usage=Meter()))
        self.assertEqual(outcome.budget.exhausted_reason, "live-call budget reached")
        self.assertEqual(outcome.budget.estimated.tokens, 2000)
        self.assertEqual(outcome.budget.estimated.cost_minor, 400)
        self.assertEqual((outcome.budget.actual.requests, outcome.budget.actual.tokens, outcome.budget.actual.cost_minor), (2, 240, 6))
        self.assertEqual(outcome.summary.counts["SKIPPED"], 1)

    def test_expired_token_is_refreshed_before_the_message_is_handled(self):
        self.server.state.reject_next_chat = True
        outcome = self.run_scenarios([make_scenario("reauth", ["hello"])])
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        self.assertEqual(list(self.server.state.sessions.values())[0].messages, ["hello"])
        self.assertGreaterEqual(self.server.state.token_requests, 2)

    def test_close_failure_still_releases_the_lease(self):
        class Closing(WebsiteTransport):
            def close(self, lease):
                super().close(lease)
                raise RuntimeError("cookie jar close failed")
        transport = Closing(self.config.base_url, self.provisioner, self.config.timeout_seconds)
        outcome = self.run_scenarios([make_scenario("close", ["hello"])], components=self.components(transport=transport))
        self.assertEqual(outcome.summary.counts["PASS"], 1)
        self.assertEqual(len(self.provisioner.cleaned), 1)
        self.assertTrue(any(json.loads(path.read_text())["phase"] == "cleanup" for path in (self.directory / "crashes").glob("*.json")))

    def test_duration_keeps_scheduling_repetitions(self):
        options = RunnerOptions(awaiting=AwaitOptions(enabled=False), load=LoadOptions(concurrency=1, duration_seconds=0.2))
        outcome = self.run_scenarios([make_scenario("fill", ["again"])], options=options)
        instances = {r.scenario_instance_id for r in outcome.summary.results if r.outcome == "PASS"}
        self.assertGreaterEqual(len(instances), 2)
        self.assertEqual(outcome.load.stop_reason, "duration_elapsed")


class OracleTests(unittest.TestCase):
    def snapshot(self, state, unavailable=()):
        return StateSnapshot(run_id="test-run", scenario_id="sessions:x", scenario_instance_id="inst", attempt=1,
                             event_id="evt-1", snapshot_id="snap-1", original_turn_index=0, request_id=None,
                             phase="before", captured_at="2026-09-29T08:30:00+00:00", state=state,
                             unavailable_sections=list(unavailable))

    def ask(self, state, unavailable=()):
        reference = ReferenceTurn(original_turn_index=1, text="reference", asks="How many Pistachio Ice Cream?")
        return SnapshotBranchOracle().pending_question(self.snapshot(state, unavailable), reference, None)

    def test_session_queue_and_explicit_projection(self):
        queue = {"ongoing_query_queue": [{"follow_up_question": ["How many Pistachio Ice Cream?"]}], "awaiting_followup_index": 0}
        self.assertEqual(self.ask(queue), "matched")
        self.assertEqual(self.ask({"chat": queue}), "matched")
        self.assertEqual(self.ask({"chat": {"pending_question": "how many pistachio ice cream"}}), "matched")
        self.assertEqual(self.ask({"ongoing_query_queue": [], "awaiting_followup_index": None}), "mismatch")
        self.assertEqual(self.ask({"sessions": {"1": "abc"}, "branch_policies": []}), "unknown")


if __name__ == "__main__":
    unittest.main()
