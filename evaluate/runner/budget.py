"""Request/session limits and a live-call budget with distinct estimate/actual totals."""
from __future__ import annotations

import threading

from evaluate.evidence.records import BudgetReport, UsageTotals
from evaluate.runner.options import BudgetOptions, LoadOptions
from evaluate.runner.ports import Usage


class BudgetExhausted(RuntimeError):
    """A configured limit would be exceeded; the caller must not dispatch."""


class BudgetTracker:
    """Reserve capacity before dispatch; record actual usage afterwards.

    Estimates use configured per-request tokens and price. Actual totals come only
    from a `UsageSource`; when none reports, actual tokens/cost stay `None` rather
    than copying the estimate.
    """

    def __init__(
        self,
        budget: BudgetOptions,
        load: LoadOptions,
        *,
        starting_requests: int = 0,
        starting_sessions: int = 0,
    ) -> None:
        self.budget, self.load = budget, load
        self._lock = threading.Lock()
        # Prior completed/intent dispatches on resume so limits span process restarts.
        self.requests = max(0, starting_requests)
        self.sessions = max(0, starting_sessions)
        self.actual_requests = 0
        self.actual_tokens: int | None = None
        self.actual_cost_minor: int | None = None
        self.actual_currency = None
        self.exhausted_reason: str | None = None

    # -- limits ---------------------------------------------------------------------
    def reserve_session(self) -> None:
        with self._lock:
            if self.load.max_sessions is not None and self.sessions >= self.load.max_sessions:
                self._exhaust("session limit reached")
            self.sessions += 1

    def reserve_request(self) -> None:
        with self._lock:
            projected = self.requests + 1
            if self.load.max_requests is not None and projected > self.load.max_requests:
                self._exhaust("request limit reached")
            if self.budget.max_live_calls is not None and projected > self.budget.max_live_calls:
                self._exhaust("live-call budget reached")
            if self.budget.max_estimated_cost_minor is not None and self._estimated_cost(projected) > self.budget.max_estimated_cost_minor:
                self._exhaust("estimated cost budget reached")
            self.requests = projected

    def _exhaust(self, reason: str) -> None:
        self.exhausted_reason = self.exhausted_reason or reason
        raise BudgetExhausted(reason)

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None

    # -- actuals --------------------------------------------------------------------
    def record_actual(self, usage: Usage | None) -> None:
        if usage is None:
            return
        with self._lock:
            self.actual_requests += 1
            if usage.tokens is not None:
                self.actual_tokens = (self.actual_tokens or 0) + usage.tokens
            if usage.cost_minor is not None:
                self.actual_cost_minor = (self.actual_cost_minor or 0) + usage.cost_minor
                self.actual_currency = usage.currency

    # -- reporting ------------------------------------------------------------------
    def _estimated_cost(self, requests: int) -> int:
        tokens = requests * self.budget.estimated_tokens_per_request
        return (tokens * self.budget.cost_per_million_tokens_minor + 999_999) // 1_000_000

    def report(self, run_id: str) -> BudgetReport:
        with self._lock:
            estimated = UsageTotals(
                requests=self.requests, tokens=self.requests * self.budget.estimated_tokens_per_request,
                cost_minor=self._estimated_cost(self.requests) if self.budget.cost_per_million_tokens_minor else None, currency=self.budget.currency,
                source="configured estimate",
            )
            actual = UsageTotals(
                requests=self.actual_requests, tokens=self.actual_tokens, cost_minor=self.actual_cost_minor,
                currency=self.actual_currency, source="usage source" if self.actual_requests else "unavailable",
            )
            return BudgetReport(
                run_id=run_id, estimated=estimated, actual=actual,
                limits={"max_requests": self.load.max_requests, "max_sessions": self.load.max_sessions,
                        "max_live_calls": self.budget.max_live_calls,
                        "max_estimated_cost_minor": self.budget.max_estimated_cost_minor},
                exhausted=self.exhausted, exhausted_reason=self.exhausted_reason,
            )
