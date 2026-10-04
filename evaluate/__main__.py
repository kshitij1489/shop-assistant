"""Offline validation, schema export, and the website-lane run command."""
import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from evaluate.contracts.models import RunConfiguration, SCHEMAS, ScenarioPlan
from evaluate.datasets.loader import DatasetError, load_dataset, read_json, validate_model
from evaluate.identity import canonical_hash
from evaluate.scenarios.plan import PLAN_PATH, readiness
from evaluate.transcripts.__main__ import add_arguments as add_transcript_arguments

PACKAGE = Path(__file__).resolve().parent


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    """Document live runner flags. Values map onto RunnerOptions; secrets are never accepted."""
    parser.add_argument("--dataset", type=Path, default=PACKAGE.parent / "test_data")
    parser.add_argument("--config", type=Path, help="Run configuration. Required with --allow-live-chat")
    parser.add_argument("--output", type=Path, help="Evidence directory outside the repository")
    parser.add_argument("--scenario", action="append", default=[], help="Scenario id to run; repeatable")
    parser.add_argument("--cache", choices=("cold", "warm"), default="cold")
    parser.add_argument("--allow-live-chat", action="store_true",
                        help="Send loopback website turns. This can call paid models")
    parser.add_argument(
        "--workload", choices=("smoke", "acceptance", "full"),
        help="Required for live chat. smoke=small bounded run; acceptance/full=selected set",
    )
    parser.add_argument("--resume", action="store_true",
                        help="Continue an interrupted evidence directory without rewriting prior records")
    parser.add_argument("--score", action="store_true", help="Score evidence after a live run")
    parser.add_argument("--report", action="store_true", help="Score and render reports after a live run")
    parser.add_argument("--recovery-attempts", type=int, default=None,
                        help="Fresh-identity restarts after ERROR (RunnerOptions.recovery.max_attempts)")
    await_group = parser.add_mutually_exclusive_group()
    await_group.add_argument("--await", dest="await_turns", action="store_true", default=None,
                             help="Wait for projected state after each turn (default)")
    await_group.add_argument("--no-await", dest="await_turns", action="store_false",
                             help="Disable post-turn state awaiting")
    parser.add_argument("--max-wait", type=float, default=None, help="Await max wait seconds")
    parser.add_argument("--poll-interval", type=float, default=None, help="Await poll interval seconds")
    parser.add_argument(
        "--branch-mismatch", choices=("block_dependent", "continue_flagged"), default=None,
        help="Branch mismatch policy",
    )
    parser.add_argument("--concurrency", type=int, default=None, help="Parallel session workers")
    parser.add_argument("--ramp-up", type=float, default=None, help="Seconds to ramp from 1 worker to concurrency")
    parser.add_argument("--pacing", type=float, default=None, help="Minimum seconds between session starts/turns")
    parser.add_argument("--duration", type=float, default=None, help="Stop launching after this many seconds")
    parser.add_argument("--warm-up-sessions", type=int, default=None, help="First N sessions reported as warm-up")
    parser.add_argument("--max-sessions", type=int, default=None, help="Stop after this many launched sessions")
    parser.add_argument("--max-requests", type=int, default=None, help="Stop after this many reserved chat requests")
    parser.add_argument("--max-provider-sessions", type=int, default=None,
                        help="Concurrent sessions touching the fake payment/POS provider")
    parser.add_argument("--max-live-calls", type=int, default=None, help="Budget for paid model chat requests")
    parser.add_argument("--estimated-tokens-per-request", type=int, default=None)
    parser.add_argument("--cost-per-million-tokens-minor", type=int, default=None)
    parser.add_argument("--budget-currency", choices=("USD", "INR"), default=None)
    parser.add_argument("--max-estimated-cost", type=int, default=None,
                        help="Estimated cost ceiling in minor currency units")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m evaluate")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate and normalize without executing")
    validate.add_argument("--dataset", type=Path, default=PACKAGE.parent / "test_data")
    validate.add_argument(
        "--plan", type=Path, default=PLAN_PATH,
        help="Scenario plan JSON (default: evaluate/scenarios/execution.plan.json)",
    )
    validate.add_argument("--config", type=Path)
    validate.add_argument("--output", type=Path, help="Optional normalized bundle JSON")
    validate.add_argument(
        "--require-ready", action="store_true",
        help="Exit 2 if any scenario has setup or readiness blockers (including duplicate address labels)",
    )
    schemas = commands.add_parser("schemas", help="Export versioned JSON Schemas")
    schemas.add_argument("--output", type=Path, default=PACKAGE / "contracts" / "schemas")
    run = commands.add_parser("run", help="Compose the website lane; chat is off unless --allow-live-chat")
    _add_run_options(run)
    preflight_cmd = commands.add_parser("preflight", help="Check live-run readiness without sending chat")
    preflight_cmd.add_argument("--dataset", type=Path, default=PACKAGE.parent / "test_data")
    preflight_cmd.add_argument("--config", type=Path, required=True)
    preflight_cmd.add_argument("--output", type=Path, required=True, help="Intended evidence directory")
    transcripts = commands.add_parser(
        "transcripts", help="Collect unscored live transcripts through the running stack; makes real model calls",
    )
    add_transcript_arguments(transcripts)
    score = commands.add_parser("score", help="Offline scoring from immutable saved evidence")
    score.add_argument("artifacts", type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.add_argument("--manual-review", type=Path)
    score.add_argument("--compare", type=Path)
    score.add_argument("--scenarios", type=Path)
    score.add_argument("--checks", type=Path)
    score.add_argument("--knowledge", type=Path)
    report = commands.add_parser("report", help="Regenerate reports from saved judgments")
    report.add_argument("report_path", type=Path)
    report.add_argument("--output", required=True, type=Path)
    inspect_cmd = commands.add_parser("inspect", help="Inspect a provisioned evaluation lease")
    inspect_cmd.add_argument("--manifest", type=Path, required=True)
    inspect_cmd.add_argument("--state-dir", type=Path, required=True)
    inspect_cmd.add_argument("--settings", help="Django settings module")
    cleanup = commands.add_parser("cleanup", help="Force-cleanup a provisioned evaluation lease")
    cleanup.add_argument("--manifest", type=Path, required=True)
    cleanup.add_argument("--state-dir", type=Path, required=True)
    cleanup.add_argument("--settings", help="Django settings module")
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            from evaluate.integration.command import execute
            return execute(args)
        if args.command == "preflight":
            from evaluate.integration.command import preflight
            return preflight(args)
        if args.command == "transcripts":
            from evaluate.transcripts.__main__ import run as transcripts_run
            return transcripts_run(args)
        if args.command == "score":
            from evaluate.reports.__main__ import main as reports_main
            argv_score = ["score", str(args.artifacts), "--output", str(args.output)]
            if args.manual_review:
                argv_score.extend(["--manual-review", str(args.manual_review)])
            if args.compare:
                argv_score.extend(["--compare", str(args.compare)])
            if args.scenarios:
                argv_score.extend(["--scenarios", str(args.scenarios)])
            if args.checks:
                argv_score.extend(["--checks", str(args.checks)])
            if args.knowledge:
                argv_score.extend(["--knowledge", str(args.knowledge)])
            return reports_main(argv_score)
        if args.command == "report":
            from evaluate.reports.__main__ import main as reports_main
            return reports_main(["render", str(args.report_path), "--output", str(args.output)])
        if args.command in {"inspect", "cleanup"}:
            from evaluate.fixtures.__main__ import main as fixtures_main
            argv_fix = [args.command, "--manifest", str(args.manifest), "--state-dir", str(args.state_dir)]
            if args.settings:
                argv_fix = ["--settings", args.settings, *argv_fix]
            return fixtures_main(argv_fix)
        if args.command == "schemas":
            args.output.mkdir(parents=True, exist_ok=True)
            for name, model in SCHEMAS.items():
                schema = model.model_json_schema()
                schema.update({"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"urn:studio-desk:evaluate:1.0.0:{name}"})
                (args.output / f"{name}.schema.json").write_text(json.dumps(schema, indent=2) + "\n")
            return 0
        plan = validate_model(ScenarioPlan, read_json(args.plan), "scenario plan")
        if args.config:
            from evaluate.integration.command import load_run_configuration
            config, _runner = load_run_configuration(args.config)
        else:
            config = None
        if config and config.scenario_plan_version != plan.version:
            raise DatasetError("configuration and plan versions differ")
        if config and Path(config.dataset_directory).resolve() != args.dataset.resolve():
            raise DatasetError("configuration dataset_directory and --dataset differ")
        bundle = load_dataset(args.dataset, plan)
        readiness_blockers = {
            scenario.scenario_id: readiness(scenario)
            for scenario in bundle.scenarios
            if readiness(scenario)
        }
        report = {
            "schema_version": "1.0.0", "valid": True, "counts": bundle.counts,
            "ready_scenarios": len(bundle.scenarios) - len(readiness_blockers),
            "blocked_scenarios": len(readiness_blockers),
            "blockers": {s.scenario_id: [b.model_dump() for b in s.blockers] for s in bundle.blocked},
            "readiness": readiness_blockers,
            "warnings": [w.model_dump() for w in bundle.warnings],
            "dataset_hashes": bundle.dataset_hashes, "scenario_plan_version": plan.version,
            "scenario_plan_hash": canonical_hash(plan),
            "configuration_hash": canonical_hash(config) if config else None,
        }
        if args.output:
            args.output.write_text(json.dumps({**report, "scenarios": [s.model_dump() for s in bundle.scenarios]}, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 2 if args.require_ready and readiness_blockers else 0
    except (DatasetError, ValidationError, OSError, ValueError) as exc:
        # Never include Pydantic input values, credentials or arbitrary exception dumps.
        message = str(exc) if isinstance(exc, DatasetError) else "invalid input or inaccessible artifact path"
        print(json.dumps({"schema_version": "1.0.0", "valid": False, "error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
