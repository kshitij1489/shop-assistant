"""`python -m evaluate run` composes the website lane. Chat stays off without a flag."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from evaluate.integration.location import evaluation_location_provider

from evaluate.contracts.models import Issue, NormalizedScenario, RunConfiguration
from evaluate.datasets.loader import DatasetError, read_json, validate_model
from evaluate.runner.options import (
    AwaitOptions, BranchOptions, BudgetOptions, LoadOptions, RecoveryOptions, RunnerOptions,
)
from evaluate.scenarios.plan import load_plan, readiness

REPOSITORY = Path(__file__).resolve().parents[2]
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

SMOKE_SCENARIO_CAP = 5
SMOKE_CONCURRENCY = 1
SMOKE_MAX_LIVE_CALLS = 40
SMOKE_MAX_REQUESTS = 40


def execute(args) -> int:
    try:
        if not getattr(args, "allow_live_chat", False):
            print(json.dumps(stitch_report(Path(args.dataset)), indent=2))
            return 0
        return _live(args)
    except (DatasetError, ValidationError, OSError, ValueError):
        print(json.dumps({"schema_version": "1.0.0", "valid": False, "error": "invalid evaluation input"}),
              file=sys.stderr)
        return 2


def stitch_report(dataset: Path) -> dict:
    """Describe the wired lane and the scenarios that stay blocked."""
    _plan, bundle = load_plan(dataset)
    blocked = [scenario.scenario_id for scenario in bundle.scenarios if readiness(scenario)]
    return {
        "schema_version": "1.0.0",
        "live_chat": False,
        "paid_model_calls": False,
        "settings_module": "evaluate.integration.settings",
        "wired": [
            "LeaseCredentials", "WebsiteTransport", "RoutedControls",
            "StateInspector", "JournalUsage",
        ],
        "blocked_scenarios": blocked,
        "note": "Blocked scenarios stay selected and record BLOCKED outcomes. Source facts are not edited.",
    }


def preflight(args) -> int:
    """Run dedicated environment preflight plus CLI-only plan/dataset checks."""
    try:
        if args.config is None or args.output is None:
            print("Preflight requires --config and --output.", file=sys.stderr)
            return 2
        config, _runner = load_run_configuration(args.config)
        output = Path(args.output)
        dataset = Path(getattr(args, "dataset", None) or config.dataset_directory)
        cli_blockers: list[dict[str, Any]] = []
        readiness_blockers: dict[str, Any] = {}

        plan, bundle = load_plan(dataset)
        if Path(config.dataset_directory).resolve() != dataset.resolve():
            cli_blockers.append({
                "code": "dataset_mismatch",
                "message": "Configuration dataset_directory and --dataset differ.",
            })
        if config.scenario_plan_version != plan.version:
            cli_blockers.append({
                "code": "plan_version",
                "message": "Configuration and execution plan versions differ.",
            })
        readiness_blockers = {
            scenario.scenario_id: readiness(scenario)
            for scenario in bundle.scenarios
            if readiness(scenario)
        }
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evaluate.integration.settings")
        os.environ["EVALUATION_EVIDENCE_ROOT"] = str(output.resolve())
        os.environ["EVALUATION_EVIDENCE_DIR"] = str(output.resolve())
        import django
        django.setup()
        from evaluate.integration.preflight import run_preflight

        env_report = run_preflight(base_url=config.base_url, evidence_directory=output)
        blockers = list(env_report.get("blockers") or []) + cli_blockers
        # Dedupe overlapping base_url / evidence codes already covered by run_preflight.
        seen: set[str] = set()
        unique_blockers: list[dict[str, Any]] = []
        for row in blockers:
            code = str(row.get("code") or "")
            if code in seen:
                continue
            seen.add(code)
            unique_blockers.append(row)

        report: dict[str, Any] = {
            "schema_version": "1.0.0",
            "valid": not unique_blockers,
            "blockers": unique_blockers,
            "checks": env_report.get("checks") or {},
        }
        if readiness_blockers:
            report["readiness"] = readiness_blockers
        print(json.dumps(report, indent=2))
        return 0 if report["valid"] else 2
    except (DatasetError, ValidationError, OSError, ValueError):
        print(json.dumps({"schema_version": "1.0.0", "valid": False, "error": "invalid evaluation input"}),
              file=sys.stderr)
        return 2


RUNNER_FIELDS = frozenset({
    "workload", "recovery_attempts", "await_turns", "max_wait", "poll_interval", "branch_mismatch",
    "concurrency", "ramp_up", "pacing", "duration", "warm_up_sessions", "max_sessions", "max_requests",
    "max_provider_sessions", "max_live_calls", "max_estimated_cost",
    "estimated_tokens_per_request", "cost_per_million_tokens_minor", "budget_currency",
})


def load_run_configuration(path) -> tuple[RunConfiguration, dict]:
    """Load a run configuration. Optional `runner` holds non-secret workload controls."""
    raw = read_json(Path(path))
    if not isinstance(raw, dict):
        raise ValueError("invalid evaluation input")
    runner = raw.pop("runner", None) or {}
    if not isinstance(runner, dict) or set(runner) - RUNNER_FIELDS:
        raise ValueError("invalid evaluation input")
    return validate_model(RunConfiguration, raw, "run configuration"), runner


def _live(args) -> int:
    if args.config is None or args.output is None:
        print("Live chat requires --config and --output.", file=sys.stderr)
        return 2
    config, runner_doc = load_run_configuration(args.config)
    args.runner_doc = runner_doc
    if not getattr(args, "workload", None):
        args.workload = runner_doc.get("workload")
    output = Path(args.output)
    if urlsplit(config.base_url).hostname not in LOOPBACK:
        print("Live evaluation chat is limited to a loopback website.", file=sys.stderr)
        return 2
    if _inside_repository(output):
        print("Live evidence must be written outside the repository.", file=sys.stderr)
        return 2
    if Path(config.dataset_directory).resolve() != Path(args.dataset).resolve():
        print("Configuration dataset_directory and --dataset differ.", file=sys.stderr)
        return 2
    if args.workload not in {"smoke", "acceptance", "full"}:
        print("Live chat requires --workload smoke|acceptance|full, or runner.workload in the config.", file=sys.stderr)
        return 2
    from uuid import uuid4
    if getattr(args, "resume", False):
        saved = read_json(output / "manifest.json")
        config = config.model_copy(update={"run_id": saved["run_id"]})
    else:
        config = config.model_copy(update={"run_id": str(uuid4())})
        os.environ["EVALUATION_EVIDENCE_ROOT"] = str(output.resolve())
        output = output / config.run_id
    if getattr(args, "resume", False):
        os.environ["EVALUATION_EVIDENCE_ROOT"] = str(output.resolve().parent)
    return _dispatch(args, config, output)


def _dispatch(args, config: RunConfiguration, output: Path) -> int:
    os.environ["EVALUATION_EVIDENCE_DIR"] = str(output.resolve())
    os.environ["EVALUATION_RUN_ID"] = config.run_id
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evaluate.integration.settings")
    import django
    django.setup()
    from django.conf import settings
    if settings.EVALUATION_ENABLED is not True:
        print("Evaluation controls are disabled. Use evaluate.integration.settings.", file=sys.stderr)
        return 2
    if not models_match(settings, config):
        return 2
    from evaluate.integration.preflight import run_preflight
    isolation = run_preflight(base_url=config.base_url, evidence_directory=output)
    if not isolation["valid"]:
        print(json.dumps(isolation), file=sys.stderr)
        return 2
    plan, bundle = load_plan(config.dataset_directory)
    if config.scenario_plan_version != plan.version:
        print("Configuration and execution plan versions differ.", file=sys.stderr)
        return 2
    selected = _select(bundle.scenarios, set(args.scenario or []))
    if selected is None:
        return 2
    options = _runner_options(args)
    selected = _apply_workload(args.workload, selected, options)
    if not selected:
        print("No scenarios were selected for the requested workload.", file=sys.stderr)
        return 2
    from evaluate.integration.compose import build_components
    from evaluate.integration.telemetry import install_process_journal
    from evaluate.manifest import build_manifest
    from evaluate.runner import EvaluationRunner
    from evaluate.evidence.store import EvidenceConflict

    output.mkdir(parents=True, exist_ok=True)
    install_process_journal(output, config.run_id)
    runner_options_payload = options.model_dump(mode="json")
    cache_mode = getattr(args, "cache", "cold")
    effective_models = isolation["checks"]["website"]["configuration"]["models"]
    manifest_kwargs = _manifest_extensions(build_manifest, cache_mode, runner_options_payload, effective_models)
    manifest = build_manifest(REPOSITORY, config, plan, bundle, **manifest_kwargs).model_copy(
        update={"scenario_ids": [scenario.scenario_id for scenario in selected]})
    resume = bool(getattr(args, "resume", False))
    if resume:
        from evaluate.contracts.models import RunManifest
        previous = RunManifest.model_validate(read_json(output / "manifest.json"))
        if previous.model_dump(exclude={"created_at"}) != manifest.model_dump(exclude={"created_at"}):
            raise ValueError("Resume requires the original configuration, dataset and application revision")
        manifest = previous
    _write_integration_note(
        output, cache_mode, effective_models=effective_models,
        runner_options=runner_options_payload, workload=args.workload,
    )
    (output / "preflight.json").write_text(json.dumps(isolation, indent=2) + "\n")
    components = build_components(config, output, cache_mode)
    try:
        outcome = EvaluationRunner(
            config, selected, components, output, options, manifest, dict(plan.fixture_hashes),
            resume=resume,
        ).run()
    except EvidenceConflict:
        print("Evidence directory already holds a run.", file=sys.stderr)
        return 2
    scored = False
    reported = False
    if getattr(args, "score", False) or getattr(args, "report", False):
        score_code = _score_evidence(output)
        if score_code != 0:
            return score_code
        scored = True
        if getattr(args, "report", False):
            report_code = _render_report(output)
            if report_code != 0:
                return report_code
            reported = True
    from evaluate.runner.runner import split_axis_counts
    axes = split_axis_counts(outcome.summary.results)
    print(json.dumps({
        "schema_version": "1.0.0", "live_chat": True, "paid_model_calls": True,
        "run_id": config.run_id, "evidence_directory": str(output.resolve()),
        "scored": scored, "reported": reported, "workload": args.workload,
        "counts": outcome.summary.counts,
        "execution_status": axes["execution_status"],
        "evaluation_verdict": axes["evaluation_verdict"],
        "reports": "python -m evaluate score" if not scored else str(output / "report"),
    }, indent=2))
    return 0


def _select(scenarios: list[NormalizedScenario], requested: set[str]) -> list[NormalizedScenario] | None:
    """Keep blocked scenarios selected so the runner can emit BLOCKED outcomes."""
    selected: list[NormalizedScenario] = []
    known = {scenario.scenario_id for scenario in scenarios}
    missing = sorted(requested - known)
    if missing:
        print("Unknown scenario id.", file=sys.stderr)
        return None
    for scenario in scenarios:
        if requested and scenario.scenario_id not in requested:
            continue
        runtime = readiness(scenario)
        if runtime:
            scenario = scenario.model_copy(update={"blockers": [Issue.model_validate(row) for row in runtime]})
        selected.append(scenario)
    if not selected:
        print("No scenarios were selected.", file=sys.stderr)
        return None
    return selected


def _control(args, name: str, fallback):
    """CLI flag wins; otherwise the run config `runner` object; otherwise the fallback."""
    cli = getattr(args, name, None)
    if cli is not None:
        return cli
    doc = getattr(args, "runner_doc", None) or {}
    if name in doc and doc[name] is not None:
        return doc[name]
    return fallback


def _runner_options(args) -> RunnerOptions:
    """Map CLI flags and config runner controls onto RunnerOptions. Secrets never appear here."""
    defaults = RunnerOptions()
    await_flag = _control(args, "await_turns", None)
    recovery = RecoveryOptions(
        max_attempts=_control(args, "recovery_attempts", defaults.recovery.max_attempts),
    )
    awaiting = AwaitOptions(
        enabled=defaults.awaiting.enabled if await_flag is None else bool(await_flag),
        max_wait_seconds=_control(args, "max_wait", defaults.awaiting.max_wait_seconds),
        poll_interval_seconds=_control(args, "poll_interval", defaults.awaiting.poll_interval_seconds),
    )
    branching = BranchOptions(
        on_mismatch=_control(args, "branch_mismatch", defaults.branching.on_mismatch),
    )
    load = LoadOptions(
        concurrency=_control(args, "concurrency", defaults.load.concurrency),
        ramp_up_seconds=_control(args, "ramp_up", defaults.load.ramp_up_seconds),
        pacing_seconds=_control(args, "pacing", defaults.load.pacing_seconds),
        duration_seconds=_control(args, "duration", None),
        warm_up_sessions=_control(args, "warm_up_sessions", defaults.load.warm_up_sessions),
        max_sessions=_control(args, "max_sessions", None),
        max_requests=_control(args, "max_requests", None),
        max_provider_sessions=_control(args, "max_provider_sessions", defaults.load.max_provider_sessions),
    )
    budget = BudgetOptions(
        max_live_calls=_control(args, "max_live_calls", None),
        max_estimated_cost_minor=_control(args, "max_estimated_cost", None),
        estimated_tokens_per_request=_control(args, "estimated_tokens_per_request", defaults.budget.estimated_tokens_per_request),
        cost_per_million_tokens_minor=_control(args, "cost_per_million_tokens_minor", defaults.budget.cost_per_million_tokens_minor),
        currency=_control(args, "budget_currency", defaults.budget.currency),
    )
    return RunnerOptions(recovery=recovery, awaiting=awaiting, branching=branching, load=load, budget=budget)


def _apply_workload(
    workload: str, selected: list[NormalizedScenario], options: RunnerOptions,
) -> list[NormalizedScenario]:
    """Apply smoke/acceptance/full presets without inventing secrets.

    Mutates `options` in place for smoke budgets when the operator did not override them.
    """
    if workload != "smoke":
        return selected
    defaults = LoadOptions()
    load_updates: dict[str, Any] = {}
    if options.load.concurrency == defaults.concurrency:
        load_updates["concurrency"] = SMOKE_CONCURRENCY
    if options.load.max_requests is None:
        load_updates["max_requests"] = SMOKE_MAX_REQUESTS
    if load_updates:
        options.load = options.load.model_copy(update=load_updates)
    if options.budget.max_live_calls is None:
        options.budget = options.budget.model_copy(update={"max_live_calls": SMOKE_MAX_LIVE_CALLS})
    representative = ("s01_add_pistachio", "s06_save_address", "s121_pickup_cash_end_to_end",
                      "s122_delivery_online_end_to_end")
    smoke = [s for source in representative for s in selected if s.source_id == source]
    knowledge = next((s for s in selected if s.setup_profiles == ["knowledge_only"]), None)
    if knowledge is not None:
        smoke.insert(0, knowledge)
    for scenario in selected:
        if len(smoke) >= SMOKE_SCENARIO_CAP:
            break
        if scenario not in smoke:
            smoke.append(scenario)
    return smoke[:SMOKE_SCENARIO_CAP]


def models_match(settings: Any, config: RunConfiguration) -> bool:
    """Check runner model names against the configured application models."""
    expected = {
        "chat": getattr(settings, "LLM_MODEL", None),
        "translate": getattr(settings, "LLM_TRANSLATE_MODEL", None),
        "analytics": getattr(settings, "LLM_ANALYTICS_MODEL", None),
    }
    configured = {
        "chat": config.models.chat,
        "translate": config.models.translate,
        "analytics": config.models.analytics,
    }
    mismatches = [name for name, value in configured.items() if expected[name] != value]
    if not mismatches:
        return True
    print(
        "Configured models do not match the running application "
        f"(config={configured}, application={expected}). Refusing live chat.",
        file=sys.stderr,
    )
    return False


def _effective_models(settings) -> dict[str, str | None]:
    return {
        "chat": getattr(settings, "LLM_MODEL", None),
        "translate": getattr(settings, "LLM_TRANSLATE_MODEL", None),
        "analytics": getattr(settings, "LLM_ANALYTICS_MODEL", None),
    }


def _manifest_extensions(
    build_manifest, cache_mode: str, runner_options: dict, effective_models: dict[str, str | None],
) -> dict:
    parameters = inspect.signature(build_manifest).parameters
    kwargs: dict[str, Any] = {}
    if "cache_mode" in parameters:
        kwargs["cache_mode"] = cache_mode
    if "runner_options" in parameters:
        kwargs["runner_options"] = runner_options
    if "effective_models" in parameters:
        # Prefer the ModelNames shape when values are present; otherwise keep a plain dict in the note only.
        try:
            from evaluate.contracts.models import ModelNames
            kwargs["effective_models"] = ModelNames(
                chat=effective_models["chat"],
                translate=effective_models["translate"],
                analytics=effective_models["analytics"],
            )
        except (KeyError, TypeError, ValidationError):
            pass
    return kwargs


def _write_integration_note(
    output: Path,
    cache_mode: str,
    *,
    effective_models: dict[str, str | None],
    runner_options: dict,
    workload: str,
) -> None:
    from django.conf import settings
    payload = {
        "schema_version": "1.0.0",
        "cache_mode": cache_mode,
        "evaluation_enabled": True,
        "location_provider": evaluation_location_provider(),
        "live_chat": True,
        "workload": workload,
        "effective_models": effective_models,
        "runner_options": runner_options,
    }
    (output / "integration.json").write_text(json.dumps(payload, indent=2) + "\n")


def _score_evidence(output: Path) -> int:
    scenarios = output / "scenarios.json"
    knowledge = output / "knowledge.json"
    checks = output / "checks.json"
    if not scenarios.is_file() or not (output / "manifest.json").is_file():
        print("Scoring requires manifest.json and scenarios.json in the evidence directory.", file=sys.stderr)
        return 2
    from evaluate.reports.__main__ import main as reports_main
    argv = ["score", str(output), "--output", str(output / "report"), "--scenarios", str(scenarios)]
    if knowledge.is_file():
        argv.extend(["--knowledge", str(knowledge)])
    if checks.is_file():
        argv.extend(["--checks", str(checks)])
    before = set((output / "report").glob("*/report.json"))
    code = reports_main(argv)
    created = set((output / "report").glob("*/report.json")) - before
    if code == 0 and len(created) == 1:
        path = created.pop()
        (output / "latest-report.json").write_text(json.dumps({"path": str(path.relative_to(output))}) + "\n")
    return code


def _render_report(output: Path) -> int:
    pointer = output / "latest-report.json"
    if not pointer.is_file():
        return 2
    report_json = output / read_json(pointer)["path"]
    if not report_json.is_file():
        print("Rendering requires a scored report.json; run with --score first or ensure scoring succeeded.", file=sys.stderr)
        return 2
    from evaluate.reports.__main__ import main as reports_main
    return reports_main(["render", str(report_json), "--output", str(output / "report")])


def _inside_repository(path: Path) -> bool:
    try:
        path.resolve().relative_to(REPOSITORY)
    except ValueError:
        return False
    return True
