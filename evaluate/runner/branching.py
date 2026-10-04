"""Clarification-branch verification and isolated continuation planning."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from evaluate.contracts.models import NormalizedScenario, ReferenceTurn, ScenarioAction, StateSnapshot, Turn
from evaluate.runner.context import RunContext

BranchDecision = Literal["matched", "mismatch", "unknown", "not_applicable"]


def referenced_question(scenario: NormalizedScenario, turn: Turn) -> ReferenceTurn | None:
    """The documented assistant entry related to this scripted turn, if any."""
    index = turn.answers_ask_from_turn if turn.answers_ask_from_turn is not None else turn.ignores_ask_from_turn
    if index is None:
        return None
    for reference in scenario.references:
        if reference.original_turn_index == index:
            return reference
    return None


def required_question(scenario: NormalizedScenario, turn: Turn) -> ReferenceTurn | None:
    """Return a reference only when its conversational state is a prerequisite.

    Answer/ignore links also describe contextual relationships used by review.
    They do not, by themselves, mean the exact reference task must remain open.
    """
    if turn.branch_dependency == "context_only":
        return None
    return referenced_question(scenario, turn)


def check_branch(context: RunContext, scenario: NormalizedScenario, turn: Turn,
                 snapshot: StateSnapshot, previous_reply: str | None) -> BranchDecision:
    """Decide whether the conversational state a scripted turn requires exists."""
    reference = required_question(scenario, turn)
    if reference is None:
        return "not_applicable"
    if reference.asks is None:
        return "unknown"  # loader already flags undefined_question; never infer one
    return context.components.branch_oracle.pending_question(snapshot, reference, previous_reply)


@dataclass(frozen=True)
class ContinuationPlan:
    """A separately identified instance that establishes the documented pending state."""
    scenario_instance_id: str
    start_user_turn_index: int
    parent_instance_id: str
    setup_action: ScenarioAction


def continuation_instance_id(run_id: str, scenario_id: str, parent_instance_id: str, user_turn_index: int) -> str:
    return str(uuid5(NAMESPACE_URL, json.dumps(["evaluate/v1/continuation", run_id, scenario_id, parent_instance_id, user_turn_index])))


def plan_continuation(context: RunContext, scenario: NormalizedScenario, turn: Turn,
                      parent_instance_id: str) -> ContinuationPlan | None:
    """Ask the planner for a reviewed fixture; reject unknown or stale fixtures."""
    planner = context.components.continuation_planner
    reference = required_question(scenario, turn)
    if planner is None or reference is None or not context.options.branching.allow_continuations:
        return None
    action = planner.plan(scenario, turn, reference)
    if action is None:
        return None
    operation = action.operation
    if operation.kind != "seed_fixture" or action.scenario_id != scenario.scenario_id:
        raise ValueError("continuation setup must be a seed_fixture action for this scenario")
    if context.fixture_hashes.get(operation.fixture_id) != operation.fixture_hash:
        raise ValueError("continuation fixture is not registered in the reviewed plan")
    return ContinuationPlan(
        scenario_instance_id=continuation_instance_id(context.config.run_id, scenario.scenario_id, parent_instance_id, turn.user_turn_index),
        start_user_turn_index=turn.user_turn_index, parent_instance_id=parent_instance_id, setup_action=action,
    )
