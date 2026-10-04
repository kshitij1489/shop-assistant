"""Run one scenario instance attempt: provision, setup, sequential turns, cleanup."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from typing import Literal

from evaluate.contracts.interfaces import Blocked, Lease
from evaluate.contracts.models import (
    EvaluationVerdict, ExecutionIdentity, ExecutionStatus, NormalizedScenario, Outcome, ScenarioAction,
    StateSnapshot, Turn,
)
from evaluate.evidence.crashes import build_crash_record, safe_message
from evaluate.evidence.records import AttemptRecord, Phase
from evaluate.evidence.redaction import redact_text
from evaluate.runner.awaiting import take_snapshot
from evaluate.runner.branching import ContinuationPlan, plan_continuation
from evaluate.runner.budget import BudgetTracker
from evaluate.runner.context import RunContext, now_iso
from evaluate.runner.transport import TokenUnavailable
from evaluate.runner.turns import TurnOutcome, TurnRunner

FailureKind = Literal["none", "blocked", "failed", "transport_unknown", "infrastructure", "budget", "evaluation", "interrupted"]
STOP_STATUSES = {"blocked", "unknown", "not_dispatched", "error", "budget"}


@dataclass
class AttemptResult:
    identity: ExecutionIdentity
    outcome: Outcome
    failure: FailureKind = "none"
    detail: str = ""
    assertion_ids: list[str] = field(default_factory=list)
    assertion_outcomes: list[Outcome] = field(default_factory=list)
    turns_completed: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    request_errors: int = 0
    completed_requests: int = 0
    ambiguous_requests: int = 0
    unsettled: bool = False
    continuation: ContinuationPlan | None = None
    warm_up: bool = False
    execution_status: ExecutionStatus | None = None
    evaluation_verdict: EvaluationVerdict | None = None
    evaluation_failure: bool = False

    @property
    def restartable(self) -> bool:
        """Fresh identity only when the same session cannot be continued safely.

        A known FAIL, an evaluation error, or a chat POST that already completed
        does not qualify. An ambiguous timeout still does, because the server
        may have committed the turn.
        """
        if self.failure in {"evaluation", "failed", "blocked", "budget", "none", "interrupted"}:
            return False
        if "FAIL" in self.assertion_outcomes or "ERROR" in self.assertion_outcomes:
            return False
        if self.failure == "transport_unknown":
            return True
        return self.failure == "infrastructure" and self.completed_requests == 0 and self.ambiguous_requests == 0


@dataclass
class AttemptRequest:
    scenario: NormalizedScenario
    scenario_instance_id: str
    attempt: int
    start_user_turn_index: int = 0
    extra_setup: list[ScenarioAction] = field(default_factory=list)
    warm_up: bool = False
    parent_instance_id: str | None = None


class ScenarioExecutor:
    def __init__(self, context: RunContext, budget: BudgetTracker) -> None:
        self.context = context
        self.turns = TurnRunner(context, budget)

    def run_attempt(self, request: AttemptRequest) -> AttemptResult:
        scenario = request.scenario
        identity = ExecutionIdentity(run_id=self.context.config.run_id, scenario_id=scenario.scenario_id,
                                     scenario_instance_id=request.scenario_instance_id, attempt=request.attempt)
        result = AttemptResult(identity, "BLOCKED", warm_up=request.warm_up)
        lease = None
        decided = False
        try:
            if scenario.blockers:
                codes = ", ".join(sorted({b.code for b in scenario.blockers}))
                self.context.event(identity, "provision", "blocked", f"scenario has unresolved blockers: {codes}")
                result.failure, result.detail = "blocked", f"blockers: {codes}"
                result.execution_status = "BLOCKED"
                decided = True
            else:
                lease = self._provision(identity, scenario, request, result)
                if lease is None:
                    decided = True
                else:
                    try:
                        self._write_provision_manifest(lease)
                        snapshot = self._setup(lease, identity, scenario, request, result)
                        if snapshot is not None:
                            self._run_turns(lease, identity, request, result, snapshot)
                    except Exception as exc:  # noqa: BLE001 - unexpected adapter failures stay inside the attempt
                        self._capture_uncaught(exc, identity, result)
                    decided = True
        finally:
            if lease is not None:
                self._cleanup(lease, identity)
        # A BaseException skips this line, so a killed process is still resumable.
        if decided:
            self._persist(result)
        return result

    def note_skip(self, item, reason: str, warm_up: bool = False) -> AttemptResult:
        """Record a configured session that was deliberately not started."""
        identity = ExecutionIdentity(run_id=self.context.config.run_id, scenario_id=item.scenario.scenario_id,
                                     scenario_instance_id=item.scenario_instance_id, attempt=item.first_attempt)
        result = AttemptResult(identity, "SKIPPED", failure="budget", detail=reason, warm_up=warm_up,
                               execution_status="SKIPPED")
        self.context.event(identity, "error", "blocked", f"not attempted: {reason}")
        self._persist(result)
        return result

    # -- lifecycle ------------------------------------------------------------------
    def _provision(self, identity: ExecutionIdentity, scenario: NormalizedScenario, request: AttemptRequest,
                   result: AttemptResult) -> Lease | None:
        origin = ""
        if request.parent_instance_id:
            origin = f" (continuation of {request.parent_instance_id} from user turn {request.start_user_turn_index})"
        self.context.event(identity, "provision", "started", "provisioning isolated tenant and fresh identity" + origin)
        try:
            lease = self.context.components.provisioner.provision(self.context.config, scenario, identity)
        except Blocked as exc:
            self.context.event(identity, "provision", "blocked", safe_message(exc))
            result.failure, result.detail = "blocked", safe_message(exc)
            result.execution_status = "BLOCKED"
            return None
        except Exception as exc:  # noqa: BLE001 - infrastructure failure becomes ERROR evidence
            self._crash(exc, "provision", identity, result)
            return None
        self.context.event(identity, "provision", "succeeded", "lease acquired")
        return lease

    def _setup(self, lease: Lease, identity: ExecutionIdentity, scenario: NormalizedScenario,
               request: AttemptRequest, result: AttemptResult) -> StateSnapshot | None:
        actions = [a for a in scenario.actions if a.original_turn_index is None] + list(request.extra_setup)
        for action in actions:
            try:
                self.context.evidence.write(self.context.components.controls.apply(lease, identity, action))
                self._write_provision_manifest(lease)
            except Blocked as exc:
                self.context.event(identity, "action", "blocked", safe_message(exc), action_id=action.action_id)
                result.failure, result.detail = "blocked", f"setup action {action.action_id}: {safe_message(exc)}"
                result.execution_status = "BLOCKED"
                return None
            except Exception as exc:  # noqa: BLE001
                self._crash(exc, "setup_action", identity, result)
                return None
        authenticate = getattr(self.context.components.transport, "authenticate", None)
        try:
            if authenticate is not None:
                authenticate(lease)
            return take_snapshot(self.context, lease, identity, None, None, "setup")
        except Blocked as exc:
            self.context.event(identity, "provision", "blocked", safe_message(exc))
            result.failure, result.detail = "blocked", safe_message(exc)
            result.execution_status = "BLOCKED"
            return None
        except TokenUnavailable as exc:
            result.outcome, result.failure = "ERROR", "infrastructure"
            result.execution_status = "ERROR"
            result.detail = f"token request failed during setup: {exc}"
            self.context.event(identity, "provision", "failed", result.detail)
            return None
        except Exception as exc:  # noqa: BLE001
            self._crash(exc, "setup_snapshot", identity, result)
            return None

    def _run_turns(self, lease: Lease, identity: ExecutionIdentity, request: AttemptRequest,
                   result: AttemptResult, latest: StateSnapshot) -> None:
        scenario, previous_reply = request.scenario, None
        turns = scenario.turns[request.start_user_turn_index:]
        gap = self.context.options.load.pacing_seconds
        for position, turn in enumerate(turns):
            if position and gap:
                self.context.stop_event.wait(gap)
            if self.context.stop_event.is_set():
                self._block_remaining(identity, turns[position:], "run stopped before this turn")
                result.failure, result.detail = "interrupted", "run stopped; resume with a fresh attempt"
                result.outcome = "INTERRUPTED"
                result.execution_status = "INTERRUPTED"
                return
            outcome = self.turns.run(lease, identity, scenario, turn, previous_reply, request.warm_up, latest)
            self._absorb(result, outcome)
            if self.context.stop_event.is_set() and outcome.unsettled:
                self._block_remaining(identity, turns[position + 1:], "run stopped during settlement")
                result.failure, result.detail = "interrupted", "run stopped during settlement"
                result.outcome = result.execution_status = "INTERRUPTED"
                return
            if outcome.snapshots:
                latest = outcome.snapshots[-1]
            if outcome.status in STOP_STATUSES:
                self._finish_stopped(identity, request, turn, turns[position + 1:], outcome, result)
                return
            previous_reply = outcome.reply
        result.outcome = self._final_outcome(result)

    def _finish_stopped(self, identity: ExecutionIdentity, request: AttemptRequest, turn: Turn, remaining: list[Turn],
                        outcome: TurnOutcome, result: AttemptResult) -> None:
        mapping: dict[str, FailureKind] = {"blocked": "blocked", "unknown": "transport_unknown",
                                           "not_dispatched": "infrastructure", "error": "infrastructure", "budget": "budget"}
        # Evaluator exceptions no longer stop the script; infrastructure still does.
        result.failure = mapping[outcome.status]
        result.detail = f"user turn {turn.user_turn_index}: {outcome.detail}"
        if "FAIL" in result.assertion_outcomes:
            result.outcome, result.failure = "FAIL", "failed"
            result.execution_status = "ERROR" if outcome.status in {"unknown", "not_dispatched", "error"} else "BLOCKED"
            result.evaluation_verdict = "FAIL"
        elif result.failure == "budget":
            result.outcome = "SKIPPED"
            result.execution_status = "SKIPPED"
        elif result.failure in ("transport_unknown", "infrastructure"):
            result.outcome = "ERROR"
            result.execution_status = "ERROR"
        else:
            result.outcome = "BLOCKED"
            result.execution_status = "BLOCKED"
        self._block_remaining(identity, remaining, f"dependent on user turn {turn.user_turn_index}, which did not complete")
        diverged = outcome.branch in ("mismatch", "unknown") and outcome.status == "blocked"
        if diverged and request.parent_instance_id is None:  # a continuation never spawns another
            self._plan_continuation(identity, request.scenario, turn, result)

    def _plan_continuation(self, identity: ExecutionIdentity, scenario: NormalizedScenario, turn: Turn,
                           result: AttemptResult) -> None:
        try:
            result.continuation = plan_continuation(self.context, scenario, turn, identity.scenario_instance_id)
        except ValueError as exc:
            self.context.event(identity, "branch_mismatch", "blocked", f"continuation rejected: {safe_message(exc)}",
                               original_turn_index=turn.original_turn_index, user_turn_index=turn.user_turn_index)
            return
        if result.continuation is not None:
            self.context.event(identity, "branch_mismatch", "started",
                               f"continuation {result.continuation.scenario_instance_id} will establish the documented state",
                               original_turn_index=turn.original_turn_index, user_turn_index=turn.user_turn_index)

    def _block_remaining(self, identity: ExecutionIdentity, remaining: list[Turn], reason: str) -> None:
        for turn in remaining:
            self.context.event(identity, "error", "blocked", f"turn not attempted: {reason}",
                               original_turn_index=turn.original_turn_index, user_turn_index=turn.user_turn_index)

    def _write_provision_manifest(self, lease: Lease) -> None:
        inspect = getattr(self.context.components.provisioner, "inspect", None)
        if not callable(inspect):
            return
        manifest = inspect(lease)
        path = self.context.evidence.directory / f"provision-{lease.handle}.json"
        temporary = path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False, sort_keys=True, default=str)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def _cleanup(self, lease: Lease, identity: ExecutionIdentity) -> None:
        self.context.event(identity, "cleanup", "started", "releasing owned resources")
        failures: list[str] = []
        try:
            self._write_provision_manifest(lease)
        except Exception as exc:
            record = build_crash_record(exc, "cleanup", identity.run_id, identity)
            path = self.context.evidence.record_crash(record)
            failures.append(f"inspect: {record.exception_type}; see crashes/{path.name}")
        for step in (self.context.components.transport.close, self.context.components.provisioner.cleanup):
            try:
                step(lease)
            except Exception as exc:  # noqa: BLE001 - one failure must not skip the other release
                record = build_crash_record(exc, "cleanup", identity.run_id, identity)
                path = self.context.evidence.record_crash(record)
                failures.append(f"{step.__name__}: {record.exception_type}; see crashes/{path.name}")
        status = "failed" if failures else "succeeded"
        self.context.event(identity, "cleanup", status, "; ".join(failures) or "owned resources released")

    # -- aggregation -----------------------------------------------------------------
    def _absorb(self, result: AttemptResult, outcome: TurnOutcome) -> None:
        result.assertion_ids.extend(a.assertion_id for a in outcome.assertions)
        result.latencies_ms.extend(outcome.latencies_ms)
        result.request_errors += outcome.request_errors
        result.completed_requests += outcome.completed_requests
        result.ambiguous_requests += int(outcome.status == "unknown")
        result.unsettled = result.unsettled or outcome.unsettled
        result.assertion_outcomes.extend(a.outcome for a in outcome.assertions)
        if outcome.evaluation_failure:
            result.evaluation_failure = True
            result.evaluation_verdict = outcome.evaluation_verdict or "NEEDS_REVIEW"
        if outcome.status == "completed":
            result.turns_completed += 1

    def _final_outcome(self, result: AttemptResult) -> Outcome:
        """Execution completion never implies an evaluation PASS."""
        outcomes = set(result.assertion_outcomes)
        if "FAIL" in outcomes:
            result.failure = "failed"
            result.execution_status = "COMPLETED"
            result.evaluation_verdict = "FAIL"
            return "FAIL"
        if result.evaluation_failure:
            result.failure = "evaluation"
            result.execution_status = "COMPLETED"
            result.evaluation_verdict = "NEEDS_REVIEW"
            result.detail = result.detail or "evaluator raised during scoring; subsequent turns still ran"
            return "NEEDS_REVIEW"
        if "ERROR" in outcomes or "NEEDS_REVIEW" in outcomes:
            result.failure = "evaluation"
            result.execution_status = "COMPLETED"
            result.evaluation_verdict = "NEEDS_REVIEW"
            return "NEEDS_REVIEW"
        if "BLOCKED" in outcomes or result.unsettled:
            result.failure = "blocked"
            result.execution_status = "COMPLETED"
            result.evaluation_verdict = "BLOCKED"
            if result.unsettled:
                result.detail = "asynchronous payment or POS outcome did not settle"
            else:
                result.detail = "an assertion lacked required evidence"
            return "BLOCKED"
        if self.context.components.evaluator is None:
            result.failure = "none"
            result.detail = "no evaluator configured; execution evidence recorded without scoring"
            result.execution_status = "COMPLETED"
            result.evaluation_verdict = None
            return "COMPLETED"
        result.execution_status = "COMPLETED"
        result.evaluation_verdict = "PASS"
        return "PASS"

    def _capture_uncaught(self, exc: BaseException, identity: ExecutionIdentity, result: AttemptResult) -> None:
        self._crash(exc, "attempt", identity, result)
        if "FAIL" in result.assertion_outcomes:
            result.outcome, result.failure = "FAIL", "failed"
            result.execution_status = "ERROR"
            result.evaluation_verdict = "FAIL"

    def _persist(self, result: AttemptResult) -> None:
        """Record the decision after cleanup. A crash before this line stays resumable."""
        identity = result.identity
        pending = result.failure == "interrupted" or (
            result.restartable and identity.attempt < self.context.options.recovery.max_attempts)
        self.context.evidence.record_attempt(AttemptRecord(
            run_id=identity.run_id, scenario_id=identity.scenario_id,
            scenario_instance_id=identity.scenario_instance_id, attempt=identity.attempt,
            outcome=result.outcome, execution_status=result.execution_status,
            evaluation_verdict=result.evaluation_verdict,
            assertion_ids=list(result.assertion_ids), failure=result.failure,
            detail=redact_text(result.detail)[:1000], restart_pending=pending, warm_up=result.warm_up,
            recorded_at=now_iso(),
        ))

    def _crash(self, exc: BaseException, phase: Phase, identity: ExecutionIdentity, result: AttemptResult) -> None:
        record = build_crash_record(exc, phase, identity.run_id, identity)
        path = self.context.evidence.record_crash(record)
        self.context.event(identity, "error", "failed", f"{record.exception_type} during {phase}; see crashes/{path.name}")
        result.outcome, result.failure = "ERROR", "infrastructure"
        result.execution_status = "ERROR"
        result.detail = f"{phase}: {record.exception_type}"
