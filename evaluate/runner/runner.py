"""Facade: turn normalized scenarios plus adapters into durable evidence and a summary."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading

from evaluate.contracts.models import (
    EvaluationSummary, ExecutionIdentity, NormalizedScenario, RunConfiguration, RunManifest, ScenarioResult,
)
from evaluate.datasets.loader import read_json
from evaluate.evidence.records import BudgetReport, LoadReport
from evaluate.evidence.store import EvidenceConflict, EvidenceStore
from evaluate.identity import instance_id
from evaluate.runner.budget import BudgetTracker
from evaluate.runner.context import Components, RunContext
from evaluate.runner.options import RunnerOptions
from evaluate.runner.recovery import RecoveryReport, inspect_evidence, mark_interrupted
from evaluate.runner.scenario import AttemptResult, ScenarioExecutor
from evaluate.runner.scheduler import Scheduler, WorkItem

# Contract evidence that identifies a prior run. Telemetry journals (application-*.jsonl)
# are process-local and must not block a brand-new live start in the same directory.
CONTRACT_JOURNALS = (
    "events.jsonl", "turns.jsonl", "snapshots.jsonl", "assertions.jsonl",
    "dispatch.jsonl", "attempts.jsonl",
)


@dataclass
class RunOutcome:
    summary: EvaluationSummary
    load: LoadReport
    budget: BudgetReport
    recovery: RecoveryReport
    attempts: list[AttemptResult] = field(default_factory=list)
    skipped_finished_instances: list[str] = field(default_factory=list)


class EvaluationRunner:
    """Execute every scenario × repetition once per attempt policy and summarize.

    The evidence directory must live outside the source tree. A directory that
    already holds evidence is only reused with `resume=True`; prior records are
    never rewritten, interrupted dispatches are marked, and finished instances
    are not re-executed.
    """

    def __init__(self, config: RunConfiguration, scenarios: list[NormalizedScenario], components: Components,
                 evidence_directory: Path, options: RunnerOptions | None = None,
                 manifest: RunManifest | None = None, fixture_hashes: dict[str, str] | None = None,
                 resume: bool = False) -> None:
        self.config, self.scenarios, self.components = config, scenarios, components
        self.options = options or RunnerOptions()
        self.manifest, self.resume = manifest, resume
        self.evidence_directory = Path(evidence_directory)
        self.fixture_hashes = dict(fixture_hashes or {})
        self._results: list[AttemptResult] = []
        self._results_lock = threading.Lock()

    def run(self) -> RunOutcome:
        store = self._open_store()
        try:
            self._write_input_artifacts()
            if self.manifest is not None:
                store.write(self.manifest)
            recovery = inspect_evidence(store)
            mark_interrupted(store, recovery)
            self._seed_completed(recovery)
            context = RunContext(self.config, self.options, self.components, store, self.fixture_hashes)
            prior_requests, prior_sessions = self._prior_budget_usage(store, recovery)
            budget = BudgetTracker(
                self.options.budget, self.options.load,
                starting_requests=prior_requests, starting_sessions=prior_sessions,
            )
            items, skipped = self._work_items(recovery)
            scheduler = Scheduler(context, budget, ScenarioExecutor(context, budget), self._collect)
            extend = self._extra_repetition if self.options.load.duration_seconds is not None else None
            load = scheduler.run(items, extend, first_extra_repetition=self.config.repetitions)
            outcome = RunOutcome(self._summary(), load, budget.report(self.config.run_id), recovery,
                                 list(self._results), skipped)
            store.write_report("load", load)
            store.write_report("budget", outcome.budget)
            # An interrupted run must stay resumable; summary.json is write-once and blocks resume.
            if load.stop_reason != "interrupted":
                store.write(outcome.summary)
            store.flush()
            return outcome
        finally:
            store.close()

    def _open_store(self) -> EvidenceStore:
        directory = self.evidence_directory
        has_evidence = directory.exists() and any((directory / name).exists() for name in CONTRACT_JOURNALS)
        if has_evidence and not self.resume:
            raise EvidenceConflict("evidence directory already holds a run; use resume=True or a new directory")
        if (directory / "summary.json").exists():
            raise EvidenceConflict("run already summarized; start a new run directory")
        return EvidenceStore(directory, self.config.run_id)

    def _write_input_artifacts(self) -> None:
        """Persist scorer inputs beside evidence, never inside the source tree."""
        directory = self.evidence_directory
        directory.mkdir(parents=True, exist_ok=True)
        scenarios_path = directory / "scenarios.json"
        knowledge_path = directory / "knowledge.json"
        checks_path = directory / "checks.json"
        if not checks_path.exists():
            from evaluate.checks.plan_checks import generate_check_specs
            specs = [spec.model_dump(mode="json") for spec in generate_check_specs(self.scenarios)]
            checks_path.write_text(json.dumps(specs, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not scenarios_path.exists():
            payload = [scenario.model_dump(mode="json") for scenario in self.scenarios]
            scenarios_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        if not knowledge_path.exists():
            knowledge_path.write_text(
                json.dumps(self._knowledge_evidence(), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

    def _knowledge_evidence(self) -> list[dict]:
        """Build `{evidence_id, source, content}` rows from setup knowledge documents."""
        names: list[str] = []
        seen: set[str] = set()
        for scenario in self.scenarios:
            for name in scenario.setup_inputs:
                if name not in seen:
                    seen.add(name)
                    names.append(name)
        root = Path(self.config.dataset_directory)
        rows: list[dict] = []
        for name in names:
            path = root / name
            content = read_json(path) if path.is_file() else {}
            stem = Path(name).stem
            rows.append({
                "evidence_id": f"knowledge:{stem}",
                "source": name,
                "content": content,
            })
        return rows

    def _prior_budget_usage(self, store: EvidenceStore, recovery: RecoveryReport) -> tuple[int, int]:
        """Initialize counters from prior dispatches so resume cannot exceed budgets."""
        if not self.resume:
            return 0, 0
        intents = [record for record in store.dispatch_records() if record.status == "intent"]
        # Finished instances will not re-reserve a session; unfinished ones will.
        finished_sessions = sum(
            1 for history in recovery.histories.values() if not history.needs_attempt
        )
        return len(intents), finished_sessions

    def _work_items(self, recovery: RecoveryReport) -> tuple[list[WorkItem], list[str]]:
        items, skipped = [], []
        for scenario_index, scenario in enumerate(self.scenarios):
            for repetition in range(self.config.repetitions):
                instance = instance_id(self.config.run_id, scenario.scenario_id, repetition)
                history = recovery.histories.get(instance)
                if history is not None and not history.needs_attempt:
                    skipped.append(instance)
                    continue
                ordinal = scenario_index * self.config.repetitions + repetition
                warm_up = ordinal < self.options.load.warm_up_sessions
                if history and history.decisions:
                    warm_up = history.decisions[max(history.decisions)].warm_up
                items.append(WorkItem(scenario, instance, history.next_attempt if history else 1,
                                      warm_up=warm_up))
        return items, skipped

    def _extra_repetition(self, repetition: int) -> list[WorkItem]:
        """Further repetitions used only to keep a duration-bounded run busy."""
        return [WorkItem(scenario, instance_id(self.config.run_id, scenario.scenario_id, repetition),
                         repetition=repetition, configured=False, warm_up=False)
                for scenario in self.scenarios]

    def _seed_completed(self, recovery: RecoveryReport) -> None:
        """Prior decisions belong in the summary of a resumed run."""
        for history in recovery.histories.values():
            for record in history.decisions.values():
                identity = ExecutionIdentity(run_id=record.run_id, scenario_id=record.scenario_id,
                                             scenario_instance_id=record.scenario_instance_id, attempt=record.attempt)
                self._results.append(AttemptResult(
                    identity, record.outcome, failure=record.failure if record.failure in {
                        "none", "blocked", "failed", "transport_unknown", "infrastructure", "budget", "evaluation", "interrupted"} else "none",
                    detail=record.detail, assertion_ids=list(record.assertion_ids), warm_up=record.warm_up,
                    execution_status=record.execution_status, evaluation_verdict=record.evaluation_verdict))

    def _collect(self, result: AttemptResult) -> None:
        with self._results_lock:
            self._results.append(result)

    def _summary(self) -> EvaluationSummary:
        with self._results_lock:
            results = [self._scenario_result(r) for r in self._results]
        counts = {key: sum(r.outcome == key for r in results) for key in ("PASS", "FAIL", "BLOCKED", "ERROR", "SKIPPED", "COMPLETED", "NEEDS_REVIEW", "INTERRUPTED")}
        return EvaluationSummary(run_id=self.config.run_id, results=results, counts=counts)

    def _scenario_result(self, attempt: AttemptResult) -> ScenarioResult:
        """Keep completed-unscored, interrupted and reviewed outcomes distinct."""
        return ScenarioResult(
            scenario_id=attempt.identity.scenario_id,
            scenario_instance_id=attempt.identity.scenario_instance_id,
            attempt=attempt.identity.attempt,
            outcome=attempt.outcome,
            assertion_ids=list(attempt.assertion_ids),
            execution_status=getattr(attempt, "execution_status", None),
            evaluation_verdict=getattr(attempt, "evaluation_verdict", None),
        )


def split_axis_counts(results: list[ScenarioResult]) -> dict[str, dict[str, int]]:
    """Count execution_status and evaluation_verdict independently of legacy Outcome.

    ``evaluation_verdict.PASS`` is scored PASS only. ``unscored`` covers rows with
    no verdict (including COMPLETED-without-evaluator). Pair with execution_status
    to distinguish completed-unscored from budget SKIPPED.
    """
    execution_keys = ("COMPLETED", "BLOCKED", "ERROR", "INTERRUPTED", "SKIPPED")
    verdict_keys = ("PASS", "FAIL", "BLOCKED", "NEEDS_REVIEW")
    return {
        "execution_status": {key: sum(r.execution_status == key for r in results) for key in execution_keys},
        "evaluation_verdict": {
            **{key: sum(r.evaluation_verdict == key for r in results) for key in verdict_keys},
            "unscored": sum(r.evaluation_verdict is None for r in results),
        },
    }
