"""Check specifications, independent of execution outcome contracts."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from evaluate.contracts.models import StrictModel, ID, Index, JsonValue

Outcome = Literal["PASS", "FAIL", "BLOCKED", "NEEDS_REVIEW"]
OUTCOMES = ("PASS", "FAIL", "BLOCKED", "NEEDS_REVIEW")
CHECK_VERSION = "saved-state-v9-empty-rejection-evidence"


class CheckSpec(StrictModel):
    check_id: ID
    scenario_id: ID
    original_turn_index: Index
    kind: Literal["equals", "basket", "totals", "ownership", "addresses",
                  "quote_invalidated", "order_count", "payment", "pos_acceptance",
                  "duplicate_effects", "mutation_claim", "unchanged", "tasks_complete", "tasks_pending"]
    criterion: str
    path: str = ""
    expected: dict[str, JsonValue] = Field(default_factory=dict)
    timing: Literal["immediate", "eventual"] = "immediate"
    deadline_ms: Annotated[float, Field(gt=0)] | None = None
    transition_at_path: str | None = None
    category: Literal["state", "money", "privacy"] = "state"

    @model_validator(mode="after")
    def deadline(self):
        if (self.timing == "eventual") != (self.deadline_ms is not None):
            raise ValueError("eventual checks require deadline_ms; immediate checks cannot have it")
        if self.transition_at_path is not None and self.timing != "eventual":
            raise ValueError("transition_at_path is only valid for eventual assertions")
        if self.kind == "equals" and (not self.path or "value" not in self.expected):
            raise ValueError("equals requires path and expected.value")
        if (self.kind == 'quote_invalidated' and 'replacement_totals' in self.expected
                and not isinstance(self.expected['replacement_totals'], dict)):
            raise ValueError('replacement_totals requires a dictionary of money expectations')
        if self.kind in {"unchanged", "tasks_complete", "tasks_pending"} and not self.path:
            raise ValueError("unchanged and task checks require a state path")
        if self.kind == "tasks_pending" and (
                not isinstance(self.expected.get("match"), dict) or not self.expected["match"]
                or type(self.expected.get("retain", False)) is not bool):
            raise ValueError("tasks_pending requires nonempty expected.match and boolean retain")
        if self.kind == "tasks_complete" and "pending_before_turn" in self.expected:
            previous = self.expected["pending_before_turn"]
            if (type(previous) is not int or not 0 <= previous < self.original_turn_index
                    or not isinstance(self.expected.get("match"), dict) or not self.expected["match"]):
                raise ValueError("pending_before_turn requires an earlier turn index and nonempty match")
        return self


class Result(StrictModel):
    assertion_id: str
    scenario_id: str
    scenario_instance_id: str
    attempt: int
    original_turn_index: int | None
    request_id: str | None
    evaluator: str
    category: str
    criterion: str
    outcome: Outcome
    explanation: str
    evidence_ids: list[str]
    item_kind: str | None = None
    item_index: int | None = None
    elapsed_to_completion_ms: float | None = None


def aggregate(outcomes):
    values = set(outcomes)
    return next((v for v in ("FAIL", "BLOCKED", "NEEDS_REVIEW") if v in values),
                "PASS" if values else "BLOCKED")
