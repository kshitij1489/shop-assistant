"""Delegate execution to the framework, then project its durable turn evidence.

The framework owns readiness, authentication, dispatch retries, provider progress,
settlement and resource lifetime. Transcript mode runs each case once, continues
on reference-branch mismatches, and disables scoring and continuation sessions.
It never restarts a conversation after an ambiguous request.
"""
from dataclasses import replace
from pathlib import Path

from evaluate.contracts.models import Issue, NormalizedScenario, RunConfiguration
from evaluate.runner import BranchOptions, Components, EvaluationRunner, RecoveryOptions, RunnerOptions
from evaluate.scenarios.plan import readiness

from evaluate.transcripts.transcript import Document, export_transcript


def _checked_case(case: NormalizedScenario) -> NormalizedScenario:
    blockers = [Issue.model_validate(row) for row in readiness(case)]
    return case.model_copy(update={"blockers": blockers})


def run_sessions(config: RunConfiguration, cases: list[NormalizedScenario],
                 components: Components, output: Path) -> Document:
    """Run unscored conversations and export replies even after interruption.

    Native journals persist each response before provider advancement. The final
    transcript is a projection of those journals, including failed safe retries;
    no alternate conversation lifecycle is maintained here.
    """
    options = RunnerOptions(
        recovery=RecoveryOptions(max_attempts=1),
        branching=BranchOptions(on_mismatch="continue_flagged", allow_continuations=False),
    )
    runner = EvaluationRunner(
        config.model_copy(update={"repetitions": 1}), [_checked_case(case) for case in cases],
        replace(components, evaluator=None), output.parent, options,
    )
    try:
        outcome = runner.run()
    finally:
        document = export_transcript(config.run_id, cases, output)
    if outcome.load.stop_reason == "interrupted":
        raise KeyboardInterrupt
    return document
