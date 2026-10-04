"""Shared per-run wiring: components, identifiers, clock and event helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from uuid import uuid4

from evaluate.contracts.interfaces import (
    Evaluator, HTTPTransport, Provisioner, ScenarioControls, StateInspector,
)
from evaluate.contracts.models import ExecutionEvent, ExecutionIdentity, RunConfiguration
from evaluate.evidence.redaction import redact_text
from evaluate.evidence.store import EvidenceStore
from evaluate.runner.options import RunnerOptions
from evaluate.runner.ports import BranchOracle, ContinuationPlanner, SettlementPolicy, UsageSource


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IdFactory:
    """Run-unique identifiers that remain unique across resumed runs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counter = 0

    def new(self, prefix: str) -> str:
        with self._lock:
            self._counter += 1
            return f"{prefix}-{self._counter:06d}-{uuid4().hex[:12]}"


@dataclass(frozen=True)
class Components:
    """Adapters supplied by the provisioning, controls, state and scoring owners."""
    provisioner: Provisioner
    controls: ScenarioControls
    transport: HTTPTransport
    inspector: StateInspector
    branch_oracle: BranchOracle
    settlement: SettlementPolicy
    evaluator: Evaluator | None = None
    continuation_planner: ContinuationPlanner | None = None
    usage: UsageSource | None = None


@dataclass
class RunContext:
    config: RunConfiguration
    options: RunnerOptions
    components: Components
    evidence: EvidenceStore
    fixture_hashes: dict[str, str] = field(default_factory=dict)
    ids: IdFactory = field(default_factory=IdFactory)
    stop_event: threading.Event = field(default_factory=threading.Event)

    def event(self, identity: ExecutionIdentity, kind: str, status: str, detail: str,
              **refs: str | int | None) -> ExecutionEvent:
        """Write and return an execution event; `refs` carry turn/request/action IDs.

        Details are runner-composed diagnostics that may quote server text, so
        they are masked here; the store still rejects anything that slips through.
        """
        event = ExecutionEvent(
            run_id=identity.run_id, scenario_id=identity.scenario_id,
            scenario_instance_id=identity.scenario_instance_id, attempt=identity.attempt,
            event_id=self.ids.new("evt"), occurred_at=now_iso(), kind=kind, status=status,
            detail=redact_text(detail)[:1000], **refs,
        )
        self.evidence.write(event)
        return event
