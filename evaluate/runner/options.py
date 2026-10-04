"""Allowlisted non-secret runner settings. Credentials never appear here."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from evaluate.contracts.models import Index, Positive, StrictModel

Seconds = Annotated[float, Field(ge=0)]

# Operations whose effects are process-wide in the mock provider or application
# (shared fault tables, frozen clocks, catalog mutation). They never run alongside
# another session.
EXCLUSIVE_OPERATIONS = frozenset({
    "freeze_clock", "lookup_control", "catalog_control", "payment_control", "reconnect", "set_delivery_fee",
})


class RecoveryOptions(StrictModel):
    max_attempts: Positive = 2  # fresh-identity restarts after ERROR, never after FAIL
    retry_not_dispatched_once: bool = True  # only when nothing reached the server
    inspect_state_after_unknown: bool = True


class AwaitOptions(StrictModel):
    enabled: bool = True
    max_wait_seconds: Seconds = 20.0
    poll_interval_seconds: Annotated[float, Field(gt=0)] = 0.5


class BranchOptions(StrictModel):
    on_mismatch: Literal["block_dependent", "continue_flagged"] = "block_dependent"
    allow_continuations: bool = True


class LoadOptions(StrictModel):
    concurrency: Positive = 1
    ramp_up_seconds: Seconds = 0.0  # linear growth from 1 worker to `concurrency`
    pacing_seconds: Seconds = 0.0  # minimum interval between session starts and between turns
    duration_seconds: Annotated[float, Field(gt=0)] | None = None  # stop launching afterwards
    warm_up_sessions: Index = 0  # first N started sessions are reported separately
    max_sessions: Positive | None = None
    max_requests: Positive | None = None
    max_provider_sessions: Positive = 1  # concurrent sessions touching the fake payment/POS provider


class BudgetOptions(StrictModel):
    max_live_calls: Positive | None = None  # chat requests that may reach paid models
    estimated_tokens_per_request: Index = 4000
    cost_per_million_tokens_minor: Index = 0  # integer minor units of `currency`
    currency: Literal["INR", "USD"] = "USD"
    max_estimated_cost_minor: Positive | None = None

    @model_validator(mode="after")
    def priced_budget(self):
        if self.max_estimated_cost_minor is not None and (
                self.cost_per_million_tokens_minor <= 0 or self.estimated_tokens_per_request <= 0):
            raise ValueError("Cost budgets require positive estimated tokens and a cost per million tokens")
        return self


class RunnerOptions(StrictModel):
    recovery: RecoveryOptions = RecoveryOptions()
    awaiting: AwaitOptions = AwaitOptions()
    branching: BranchOptions = BranchOptions()
    load: LoadOptions = LoadOptions()
    budget: BudgetOptions = BudgetOptions()
    clock_skew_reauth_seconds: Positive = 3000  # re-fetch the 1 hour tenant JWT before expiry

    @model_validator(mode="after")
    def bounded_await(self):
        if self.awaiting.enabled and self.awaiting.poll_interval_seconds > max(self.awaiting.max_wait_seconds, 0.001):
            raise ValueError("poll interval must not exceed the maximum wait")
        return self
