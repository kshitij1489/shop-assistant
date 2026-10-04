"""Runner-owned durable records that complement the canonical v1 contracts.

Canonical artifacts (`ExecutionEvent`, `TurnEvidence`, ...) forbid extra fields, so
dispatch bookkeeping, crash diagnostics and load/budget reports use these models.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from evaluate.contracts.models import ID, Index, Outcome, Positive, StrictModel, ExecutionStatus, EvaluationVerdict

DispatchStatus = Literal["intent", "not_dispatched", "in_flight_unknown", "completed", "interrupted"]
Phase = Literal["provision", "setup_action", "setup_snapshot", "branch_check", "turn_action",
                "before_snapshot", "dispatch", "after_snapshot", "await_settlement",
                "evaluate", "cleanup", "scheduler", "recovery", "attempt"]


def _offset_timestamp(value: str) -> str:
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("timestamp must have an offset")
    return value


class RunnerRecord(StrictModel):
    record_version: Literal["runner-1.0.0"] = "runner-1.0.0"


class Expectation(StrictModel):
    intent: str
    sub_intent: str
    turn_kind: str
    expected_facts: list[str]
    must_not: list[str]


class DispatchRecord(RunnerRecord):
    """One line per state change of one HTTP attempt; never rewritten.

    `intent` is written and flushed before the request leaves the process so an
    interrupted run can tell an unsent turn from an ambiguous in-flight one.
    """
    run_id: ID
    scenario_id: ID
    scenario_instance_id: ID
    attempt: Positive
    request_id: ID
    original_turn_index: Index
    user_turn_index: Index
    status: DispatchStatus
    recorded_at: str
    message: str
    expectation: Expectation
    http_status: int | None = None
    elapsed_ms: float | None = None
    transport_error: str | None = None
    error: str | None = None  # redacted diagnostic
    warm_up: bool = False

    _timestamp = field_validator("recorded_at")(_offset_timestamp)


class CrashRecord(RunnerRecord):
    crash_id: ID
    run_id: ID
    scenario_id: ID | None
    scenario_instance_id: ID | None
    attempt: Positive | None
    original_turn_index: Index | None
    user_turn_index: Index | None
    request_id: ID | None
    phase: Phase
    exception_type: str
    message: str  # redacted
    traceback: list[str]  # redacted frames
    occurred_at: str
    related_event_ids: list[ID] = Field(default_factory=list)
    related_files: list[str] = Field(default_factory=list)

    _timestamp = field_validator("occurred_at")(_offset_timestamp)


class AttemptRecord(RunnerRecord):
    """Durable decision for one attempt. Cleanup is not a decision.

    Written after the scripted result is known and resources are released. Resume
    treats a missing record as unfinished even when a cleanup event exists.
    `restart_pending` is true only when another fresh-identity attempt is still
    allowed; a later resume must not invent attempts beyond that.
    """
    run_id: ID
    scenario_id: ID
    scenario_instance_id: ID
    attempt: Positive
    outcome: Outcome
    execution_status: ExecutionStatus | None = None
    evaluation_verdict: EvaluationVerdict | None = None
    assertion_ids: list[ID] = Field(default_factory=list)
    failure: str
    detail: str
    restart_pending: bool
    warm_up: bool = False
    recorded_at: str

    _timestamp = field_validator("recorded_at")(_offset_timestamp)


class LatencySummary(StrictModel):
    count: Index
    p50_ms: float | None
    p95_ms: float | None
    max_ms: float | None


class PhaseMetrics(StrictModel):
    sessions_started: Index
    sessions_completed: Index
    requests_sent: Index
    request_errors: Index  # transport errors or HTTP >= 500
    session_errors: Index  # attempts ending in ERROR
    latency: LatencySummary


class LoadReport(RunnerRecord):
    run_id: ID
    configured_concurrency: Positive
    achieved_concurrency_max: Index
    achieved_concurrency_mean: float
    ramp_up_seconds: float
    pacing_seconds: float
    duration_seconds: float | None
    elapsed_seconds: float
    stop_reason: Literal["completed", "duration_elapsed", "request_limit", "session_limit", "budget_exhausted", "interrupted"]
    warm_up: PhaseMetrics
    measured: PhaseMetrics


class UsageTotals(StrictModel):
    requests: Index
    tokens: Index | None
    cost_minor: Index | None
    currency: Literal["INR", "USD"] | None = None
    source: str


class BudgetReport(RunnerRecord):
    run_id: ID
    estimated: UsageTotals
    actual: UsageTotals
    limits: dict[str, int | None]
    exhausted: bool
    exhausted_reason: str | None
