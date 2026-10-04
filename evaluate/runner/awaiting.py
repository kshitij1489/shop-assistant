"""Bounded polling for asynchronous payment/POS outcomes with recorded transitions."""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Callable

from evaluate.contracts.interfaces import Lease
from evaluate.contracts.models import ExecutionIdentity, NormalizedScenario, StateSnapshot
from evaluate.runner.context import RunContext


@dataclass
class AwaitOutcome:
    settled: bool
    reason: str | None  # None when settled; otherwise a persisted diagnostic
    snapshots: list[StateSnapshot] = field(default_factory=list)
    transitions: list[str] = field(default_factory=list)
    polls: int = 0


def take_snapshot(context: RunContext, lease: Lease, identity: ExecutionIdentity,
                  original_turn_index: int | None, request_id: str | None, phase: str) -> StateSnapshot:
    """Capture, persist and return one state snapshot."""
    snapshot = context.components.inspector.snapshot(lease, identity, original_turn_index, request_id, phase)
    context.evidence.write(snapshot)
    return snapshot


def await_settlement(context: RunContext, lease: Lease, identity: ExecutionIdentity,
                     original_turn_index: int | None, request_id: str | None, initial: StateSnapshot,
                     sleep: Callable[[float], None] = time.sleep,
                     scenario: NormalizedScenario | None = None) -> AwaitOutcome:
    """Poll authoritative state until nothing unexpected is pending or time runs out.

    Every observed transition is written as a `snapshot` event naming the changed
    sections; a timeout writes a `blocked` event that names what was still pending.
    Pending payment/POS that a later `payment_control` will settle is expected and
    does not wait or fail the attempt.
    """
    options = context.options.awaiting
    policy = context.components.settlement
    outcome = AwaitOutcome(settled=True, reason=None)
    pending = _unexpected_pending(policy, initial, scenario, original_turn_index)
    if not options.enabled or not pending:
        return outcome
    deadline = time.monotonic() + options.max_wait_seconds
    previous = policy.fingerprint(initial)
    context.event(identity, "snapshot", "started", f"awaiting settlement of {', '.join(pending)}",
                  original_turn_index=original_turn_index, request_id=request_id)
    while pending:
        if time.monotonic() >= deadline or context.stop_event.is_set():
            why = "run stopped" if context.stop_event.is_set() else f"timeout after {options.max_wait_seconds}s"
            outcome.settled, outcome.reason = False, f"{why}; still pending: {', '.join(pending)}"
            context.event(identity, "snapshot", "blocked", outcome.reason,
                          original_turn_index=original_turn_index, request_id=request_id)
            return outcome
        sleep(min(options.poll_interval_seconds, max(deadline - time.monotonic(), 0)))
        advance = getattr(context.components.transport, "advance", None)
        if advance is not None:
            advance(lease)
        snapshot = take_snapshot(context, lease, identity, original_turn_index, request_id, "after")
        outcome.polls += 1
        outcome.snapshots.append(snapshot)
        current = policy.fingerprint(snapshot)
        if current != previous:
            transition = f"transition observed in {', '.join(_changed(initial, snapshot, policy))}"
            outcome.transitions.append(transition)
            context.event(identity, "snapshot", "succeeded", transition,
                          original_turn_index=original_turn_index, request_id=request_id)
            previous, initial = current, snapshot
        pending = _unexpected_pending(policy, snapshot, scenario, original_turn_index)
    context.event(identity, "snapshot", "succeeded", f"settled after {outcome.polls} polls",
                  original_turn_index=original_turn_index, request_id=request_id)
    return outcome


def _unexpected_pending(policy, snapshot: StateSnapshot, scenario: NormalizedScenario | None,
                        original_turn_index: int | None) -> list[str]:
    """Prefer a settlement policy that understands scenario-scoped expectations."""
    unexpected = getattr(policy, "unexpected_pending_sections", None)
    if callable(unexpected):
        return list(unexpected(snapshot, scenario, original_turn_index))
    return list(policy.pending_sections(snapshot))


def _changed(before: StateSnapshot, after: StateSnapshot, policy) -> list[str]:
    names = sorted(set(before.state) | set(after.state))
    changed = [name for name in names if before.state.get(name) != after.state.get(name)]
    return changed or ["asynchronous sections"]
