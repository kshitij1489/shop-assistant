"""Inspect an existing evidence directory and classify interrupted work.

A dispatch `intent` with no terminal record means the process died while the
request may have been in flight. Such turns are marked `interrupted` (appended,
never rewritten) and their instance needs a fresh-identity attempt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from evaluate.contracts.models import ExecutionEvent
from evaluate.evidence.records import AttemptRecord, DispatchRecord
from evaluate.evidence.store import EvidenceStore
from evaluate.runner.context import now_iso

TERMINAL = {"not_dispatched", "in_flight_unknown", "completed", "interrupted"}


@dataclass
class InstanceHistory:
    scenario_id: str
    scenario_instance_id: str
    attempts_seen: set[int] = field(default_factory=set)
    interrupted_attempts: set[int] = field(default_factory=set)
    decisions: dict[int, AttemptRecord] = field(default_factory=dict)

    @property
    def next_attempt(self) -> int:
        seen = self.attempts_seen | set(self.decisions)
        return max(seen, default=0) + 1

    @property
    def needs_attempt(self) -> bool:
        """A cleanup event does not finish an attempt. A decision record does.

        An attempt with `restart_pending` still needs the next fresh identity.
        """
        seen = self.attempts_seen | set(self.decisions)
        latest = max(seen, default=None)
        if latest is None:
            return True
        decision = self.decisions.get(latest)
        if decision is None:
            return True
        return decision.restart_pending


@dataclass
class RecoveryReport:
    interrupted: list[DispatchRecord] = field(default_factory=list)
    histories: dict[str, InstanceHistory] = field(default_factory=dict)
    damaged: dict[str, list[int]] = field(default_factory=dict)
    truncated: list[str] = field(default_factory=list)

    def history(self, scenario_id: str, scenario_instance_id: str) -> InstanceHistory:
        return self.histories.setdefault(scenario_instance_id, InstanceHistory(scenario_id, scenario_instance_id))


def inspect_evidence(store: EvidenceStore) -> RecoveryReport:
    """Read ledgers and journals without writing anything."""
    report = RecoveryReport()
    for name, contents in store.recovered.items():
        if contents.damaged_lines:
            report.damaged[name] = list(contents.damaged_lines)
        if contents.truncated_tail:
            report.truncated.append(name)
    open_intents: dict[str, DispatchRecord] = {}
    for record in store.dispatch_records():
        report.history(record.scenario_id, record.scenario_instance_id).attempts_seen.add(record.attempt)
        if record.status == "intent":
            open_intents[record.request_id] = record
        elif record.status in TERMINAL:
            open_intents.pop(record.request_id, None)
    report.interrupted = list(open_intents.values())
    for record in report.interrupted:
        report.history(record.scenario_id, record.scenario_instance_id).interrupted_attempts.add(record.attempt)
    for record in store.attempt_records():
        report.history(record.scenario_id, record.scenario_instance_id).decisions[record.attempt] = record
    events = store.recovered.get("events.jsonl")
    for payload in (events.records if events else []):
        event = ExecutionEvent.model_validate(payload)
        report.history(event.scenario_id, event.scenario_instance_id).attempts_seen.add(event.attempt)
    return report


def mark_interrupted(store: EvidenceStore, report: RecoveryReport) -> int:
    """Append an `interrupted` record for every open intent; returns how many."""
    for record in report.interrupted:
        store.record_dispatch(record.model_copy(update={
            "status": "interrupted", "recorded_at": now_iso(),
            "error": "process ended after dispatch intent; server-side effect unknown",
        }))
    return len(report.interrupted)
