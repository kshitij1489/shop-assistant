"""Execute one scripted user turn: actions, branch check, single dispatch, evidence."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Literal

from evaluate.contracts.interfaces import Blocked, ChatRequest, ChatResponse, Lease
from evaluate.runner.transport import TokenUnavailable
from evaluate.contracts.models import (
    AssertionResult, EvaluationVerdict, ExecutionIdentity, NormalizedScenario, StateSnapshot, Turn, TurnEvidence,
)
from evaluate.evidence.crashes import build_crash_record, safe_message
from evaluate.evidence.records import DispatchRecord, Expectation, Phase
from evaluate.evidence.redaction import redact_text
from evaluate.runner.awaiting import await_settlement, take_snapshot
from evaluate.runner.branching import BranchDecision, check_branch
from evaluate.runner.budget import BudgetExhausted, BudgetTracker
from evaluate.runner.context import RunContext, now_iso

TurnStatus = Literal["completed", "blocked", "unknown", "not_dispatched", "error", "budget"]
TRANSPORT_ERRORS = {"timeout", "connection", "invalid_response"}
DISPATCH_TO_STATUS: dict[str, TurnStatus] = {
    "completed": "completed", "not_dispatched": "not_dispatched", "in_flight_unknown": "unknown",
}
Refs = dict[str, Any]


@dataclass
class TurnOutcome:
    status: TurnStatus
    detail: str = ""
    branch: BranchDecision = "not_applicable"
    evidence: TurnEvidence | None = None
    assertions: list[AssertionResult] = field(default_factory=list)
    latencies_ms: list[float] = field(default_factory=list)
    request_errors: int = 0
    snapshots: list[StateSnapshot] = field(default_factory=list)
    evaluation_failure: bool = False
    evaluation_verdict: EvaluationVerdict | None = None
    unsettled: bool = False
    completed_requests: int = 0
    persisted: bool = False

    @property
    def reply(self) -> str | None:
        return self.evidence.response_text if self.evidence else None


@dataclass
class TurnRunner:
    context: RunContext
    budget: BudgetTracker

    def run(self, lease: Lease, identity: ExecutionIdentity, scenario: NormalizedScenario, turn: Turn,
            previous_reply: str | None, warm_up: bool, prior_snapshot: StateSnapshot) -> TurnOutcome:
        # Branch check uses state from before this turn's actions, so a mismatch
        # cannot apply a mutation for a message that will not be sent.
        decision = check_branch(self.context, scenario, turn, prior_snapshot, previous_reply)
        divergence = self._record_branch(identity, turn, decision)
        if divergence is not None:
            return divergence
        blocked = self._apply_turn_actions(lease, identity, scenario, turn)
        if blocked is not None:
            return blocked
        before = take_snapshot(self.context, lease, identity, turn.original_turn_index, None, "before")
        outcome = self._dispatch(lease, identity, turn, warm_up, decision, before.snapshot_id)
        if outcome.evidence is None:
            return outcome
        # Dispatch persists the reply before the completed ledger row. Keep this
        # fallback for outcomes that returned evidence without going through it.
        if not outcome.persisted:
            self._persist_response_evidence(outcome, [before.snapshot_id])
        try:
            if outcome.status == "completed" and self.context.components.usage is not None:
                self.budget.record_actual(self.context.components.usage.usage(lease, outcome.evidence.request_id))
            snapshots = self._observe_after(lease, identity, scenario, turn, outcome, before)
        except Exception as exc:  # noqa: BLE001 - the POST already finished; do not lose that fact
            self._crash(exc, "after_snapshot", identity, turn, outcome.evidence.request_id)
            outcome.status, outcome.detail = "error", f"state inspection failed after dispatch: {type(exc).__name__}"
            return outcome
        if outcome.status != "completed":
            return outcome
        return self._evaluate(identity, scenario, turn, outcome, snapshots)

    # -- actions and branch -------------------------------------------------------
    def _apply_turn_actions(self, lease: Lease, identity: ExecutionIdentity, scenario: NormalizedScenario,
                            turn: Turn) -> TurnOutcome | None:
        for action in [a for a in scenario.actions if a.original_turn_index == turn.original_turn_index]:
            try:
                self.context.evidence.write(self.context.components.controls.apply(lease, identity, action))
            except Blocked as exc:
                self.context.event(identity, "action", "blocked", safe_message(exc), action_id=action.action_id,
                                   **_turn_refs(turn))
                return TurnOutcome("blocked", f"action {action.action_id} blocked: {safe_message(exc)}")
            except Exception as exc:  # noqa: BLE001 - adapter failures become ERROR evidence, never silence
                self._crash(exc, "turn_action", identity, turn)
                return TurnOutcome("error", f"action {action.action_id} raised {type(exc).__name__}")
        return None

    def _record_branch(self, identity: ExecutionIdentity, turn: Turn, decision: BranchDecision) -> TurnOutcome | None:
        if decision in ("matched", "not_applicable"):
            return None
        blocking = self.context.options.branching.on_mismatch == "block_dependent"
        detail = ("documented pending question is not open; actual conversation followed another branch"
                  if decision == "mismatch" else "pending-question state unavailable; branch cannot be verified")
        self.context.event(identity, "branch_mismatch", "blocked" if blocking else "failed", detail, **_turn_refs(turn))
        return TurnOutcome("blocked", detail, branch=decision) if blocking else None

    # -- dispatch -----------------------------------------------------------------
    def _dispatch(self, lease: Lease, identity: ExecutionIdentity, turn: Turn, warm_up: bool,
                  decision: BranchDecision, before_snapshot_id: str | None = None) -> TurnOutcome:
        outcome = self._dispatch_once(lease, identity, turn, warm_up, decision, before_snapshot_id)
        if outcome.status != "not_dispatched" or not self.context.options.recovery.retry_not_dispatched_once:
            return outcome
        # Nothing reached the server, so the identity is untouched: one fresh request is safe.
        if outcome.evidence is not None and not outcome.persisted:
            self.context.evidence.write(outcome.evidence)  # retain the failed attempt
            self.context.event(identity, "request", "failed", "nothing reached the server; one fresh request follows",
                               request_id=outcome.evidence.request_id, **_turn_refs(turn))
        elif outcome.evidence is not None:
            self.context.event(identity, "request", "failed", "nothing reached the server; one fresh request follows",
                               request_id=outcome.evidence.request_id, **_turn_refs(turn))
        retry = self._dispatch_once(lease, identity, turn, warm_up, decision, before_snapshot_id)
        retry.latencies_ms = outcome.latencies_ms + retry.latencies_ms
        retry.request_errors += outcome.request_errors
        return retry

    def _dispatch_once(self, lease: Lease, identity: ExecutionIdentity, turn: Turn, warm_up: bool,
                       decision: BranchDecision, before_snapshot_id: str | None = None) -> TurnOutcome:
        prepared = self._prepare_authentication(lease)
        if prepared is not None:
            return prepared
        try:
            self.budget.reserve_request()
        except BudgetExhausted as exc:
            self.context.event(identity, "error", "blocked", f"dispatch withheld: {exc}", **_turn_refs(turn))
            return TurnOutcome("budget", str(exc))
        request_id = self.context.ids.new("req")
        refs: Refs = {"request_id": request_id, **_turn_refs(turn)}
        started_event = self.context.event(identity, "request", "started", "dispatching one user turn", **refs)
        self._ledger(identity, turn, request_id, "intent", warm_up)
        self.context.evidence.flush()
        started = time.perf_counter()
        try:
            response = self.context.components.transport.send(lease, ChatRequest(identity, request_id, turn))
        except Blocked as exc:
            # Raised before the POST (authentication). The intent must still be closed.
            response = ChatResponse(None, None, (time.perf_counter() - started) * 1000.0, "connection")
            self.context.event(identity, "response", "blocked", safe_message(exc), **refs)
            outcome = TurnOutcome("blocked", safe_message(exc), branch=decision,
                                  evidence=self._evidence(identity, turn, request_id, response, decision, warm_up),
                                  latencies_ms=[response.elapsed_ms])
            return self._finish_dispatch(
                outcome, before_snapshot_id, identity, turn, request_id, "not_dispatched",
                warm_up, response, safe_message(exc))
        except Exception as exc:  # noqa: BLE001 - an unexplained transport failure is ambiguous by definition
            self._crash(exc, "dispatch", identity, turn, request_id, [started_event.event_id])
            response = ChatResponse(None, None, (time.perf_counter() - started) * 1000.0, "connection")
            self.context.event(identity, "response", "failed", f"transport raised {type(exc).__name__}", **refs)
            outcome = TurnOutcome("unknown", f"transport raised {type(exc).__name__}", branch=decision,
                                  evidence=self._evidence(identity, turn, request_id, response, decision, warm_up),
                                  latencies_ms=[response.elapsed_ms], request_errors=1)
            return self._finish_dispatch(
                outcome, before_snapshot_id, identity, turn, request_id, "in_flight_unknown",
                warm_up, response, safe_message(exc))
        return self._record_response(
            lease, identity, turn, request_id, response, warm_up, decision, refs, before_snapshot_id)

    def _record_response(self, lease: Lease, identity: ExecutionIdentity, turn: Turn, request_id: str,
                         response: ChatResponse, warm_up: bool, decision: BranchDecision, refs: Refs,
                         before_snapshot_id: str | None = None) -> TurnOutcome:
        default_state = "completed" if response.transport_error is None else "in_flight_unknown"
        state = getattr(response, "dispatch_state", default_state)
        detail = _response_detail(response, state)
        self.context.event(identity, "response", "succeeded" if state == "completed" else "failed", detail, **refs)
        errors = int(response.transport_error is not None or (response.status_code or 0) >= 500)
        outcome = TurnOutcome(DISPATCH_TO_STATUS[state], detail, branch=decision,
                              evidence=self._evidence(identity, turn, request_id, response, decision, warm_up),
                              latencies_ms=[response.elapsed_ms], request_errors=errors,
                              completed_requests=int(state == "completed"))
        # Reply hits disk before the dispatch row says the turn completed.
        self._finish_dispatch(outcome, before_snapshot_id, identity, turn, request_id, state, warm_up, response)
        return outcome

    def _finish_dispatch(self, outcome: TurnOutcome, before_snapshot_id: str | None,
                         identity: ExecutionIdentity, turn: Turn, request_id: str, state: str,
                         warm_up: bool, response: ChatResponse | None, error: str | None = None) -> TurnOutcome:
        """Persist the assistant reply, then record the dispatch status."""
        if outcome.evidence is not None and not outcome.persisted:
            snapshot_ids = [before_snapshot_id] if before_snapshot_id else []
            self._persist_response_evidence(outcome, snapshot_ids)
            outcome.persisted = True
        self._ledger(identity, turn, request_id, state, warm_up, response, error)
        return outcome

    def _prepare_authentication(self, lease: Lease) -> TurnOutcome | None:
        """Refresh the tenant token before the dispatch intent is persisted."""
        transport = self.context.components.transport
        prepare = getattr(transport, "prepare", None) or getattr(transport, "authenticate", None)
        if prepare is None:
            return None
        try:
            prepare(lease)
        except Blocked as exc:
            return TurnOutcome("blocked", safe_message(exc))
        except TokenUnavailable as exc:
            return TurnOutcome("not_dispatched", f"token request failed before dispatch: {exc}")
        return None

    def _evidence(self, identity: ExecutionIdentity, turn: Turn, request_id: str, response: ChatResponse,
                  decision: BranchDecision, warm_up: bool = False) -> TurnEvidence:
        error = response.transport_error
        reply = None if response.response_text is None else redact_text(response.response_text)
        return TurnEvidence(
            run_id=identity.run_id, scenario_id=identity.scenario_id, scenario_instance_id=identity.scenario_instance_id,
            attempt=identity.attempt, event_id=self.context.ids.new("evt"), original_turn_index=turn.original_turn_index,
            user_turn_index=turn.user_turn_index, request_id=request_id, message=redact_text(turn.text),
            sent_message=redact_text(getattr(response, "sent_message", None) or turn.text),
            response_text=reply, http_status=response.status_code, elapsed_ms=response.elapsed_ms,
            response_error=redact_text(getattr(response, "error_text", None)) if getattr(response, "error_text", None) else None,
            snapshot_ids=[], branch="mismatch" if decision in ("mismatch", "unknown") else decision,
            transport_error=None if error is None else (error if error in TRANSPORT_ERRORS else "connection"),
            warm_up=warm_up,
        )

    def _persist_response_evidence(self, outcome: TurnOutcome, snapshot_ids: list[str]) -> None:
        """Durably record the assistant reply before fallible observation work."""
        evidence = outcome.evidence
        assert evidence is not None
        outcome.evidence = evidence.model_copy(update={"snapshot_ids": list(snapshot_ids)})
        self.context.evidence.write(outcome.evidence)
        self.context.evidence.flush()

    # -- after the request ---------------------------------------------------------
    def _observe_after(self, lease: Lease, identity: ExecutionIdentity, scenario: NormalizedScenario,
                       turn: Turn, outcome: TurnOutcome, before: StateSnapshot) -> list[StateSnapshot]:
        """Snapshot after the request and await unexpected settlement.

        TurnEvidence was already persisted with the reply; later snapshots are
        written as their own artifacts and only attached in memory for scoring.
        """
        evidence = outcome.evidence
        assert evidence is not None
        snapshots = [before]
        if outcome.status == "unknown" and not self.context.options.recovery.inspect_state_after_unknown:
            outcome.snapshots = snapshots
            return snapshots
        if outcome.status == "completed":
            advance = getattr(self.context.components.transport, "advance", None)
            if advance is not None:
                advance(lease)
        after = take_snapshot(self.context, lease, identity, turn.original_turn_index, evidence.request_id, "after")
        snapshots.append(after)
        if outcome.status == "completed":
            awaited = await_settlement(
                self.context, lease, identity, turn.original_turn_index, evidence.request_id, after,
                scenario=scenario,
            )
            if not awaited.settled:
                outcome.unsettled = True
                outcome.detail = f"{outcome.detail}; {awaited.reason}"
            snapshots.extend(awaited.snapshots)
        outcome.snapshots = snapshots
        # In-memory only: evidence store rejects a second TurnEvidence for this request.
        outcome.evidence = evidence.model_copy(update={"snapshot_ids": [s.snapshot_id for s in snapshots]})
        return snapshots

    def _evaluate(self, identity: ExecutionIdentity, scenario: NormalizedScenario, turn: Turn, outcome: TurnOutcome,
                  snapshots: list[StateSnapshot]) -> TurnOutcome:
        evaluator = self.context.components.evaluator
        if evaluator is None or outcome.evidence is None:
            return outcome
        try:
            results = list(evaluator.evaluate(scenario, turn, outcome.evidence, snapshots))
            for result in results:  # unsafe or conflicting assertion artifacts are evaluator failures
                self.context.evidence.write(result)
        except Exception as exc:  # noqa: BLE001 - scoring failure must not become execution ERROR
            self._crash(exc, "evaluate", identity, turn, outcome.evidence.request_id, [outcome.evidence.event_id])
            outcome.evaluation_failure = True
            outcome.evaluation_verdict = "NEEDS_REVIEW"
            outcome.detail = f"{outcome.detail}; evaluator raised {type(exc).__name__}".strip("; ")
            # Scoring failure does not stop the scripted conversation.
            return outcome
        for result in results:
            self.context.event(identity, "assertion", "succeeded" if result.outcome == "PASS" else "failed",
                               f"{result.assertion_id}: {result.outcome}", request_id=outcome.evidence.request_id,
                               **_turn_refs(turn))
        outcome.assertions = results
        return outcome

    # -- bookkeeping ---------------------------------------------------------------
    def _ledger(self, identity: ExecutionIdentity, turn: Turn, request_id: str, status: str, warm_up: bool,
                response: ChatResponse | None = None, error: str | None = None) -> None:
        self.context.evidence.record_dispatch(DispatchRecord(
            run_id=identity.run_id, scenario_id=identity.scenario_id, scenario_instance_id=identity.scenario_instance_id,
            attempt=identity.attempt, request_id=request_id, original_turn_index=turn.original_turn_index,
            user_turn_index=turn.user_turn_index, status=status, recorded_at=now_iso(), message=redact_text(turn.text),
            expectation=Expectation(intent=turn.intent, sub_intent=turn.sub_intent, turn_kind=turn.turn_kind,
                                    expected_facts=[redact_text(f) for f in turn.expected_facts],
                                    must_not=[redact_text(f) for f in turn.must_not]),
            http_status=response.status_code if response else None,
            elapsed_ms=response.elapsed_ms if response else None,
            transport_error=response.transport_error if response else None, error=error, warm_up=warm_up,
        ))

    def _crash(self, exc: BaseException, phase: Phase, identity: ExecutionIdentity, turn: Turn,
               request_id: str | None = None, related_event_ids: list[str] | None = None) -> None:
        record = build_crash_record(exc, phase, identity.run_id, identity, turn.original_turn_index,
                                    turn.user_turn_index, request_id, related_event_ids)
        path = self.context.evidence.record_crash(record)
        self.context.event(identity, "error", "failed", f"{record.exception_type} during {phase}; see crashes/{path.name}",
                           request_id=request_id, **_turn_refs(turn))


def _turn_refs(turn: Turn) -> Refs:
    return {"original_turn_index": turn.original_turn_index, "user_turn_index": turn.user_turn_index}


def _response_detail(response: ChatResponse, state: str) -> str:
    if state != "completed":
        return f"{state}: {response.transport_error}"
    if response.status_code == 200 and response.response_text is not None:
        return f"HTTP 200 reply of {len(response.response_text)} characters"
    error_text = getattr(response, "error_text", None)
    return f"HTTP {response.status_code}: {error_text or response.transport_error or 'no reply text'}"
