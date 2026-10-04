"""python -m evaluate transcripts sessions sanity  (or python -m evaluate.transcripts ...)"""

import argparse
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from evaluate.integration.location import evaluation_location_provider
from evaluate.contracts.models import NormalizedScenario, RunConfiguration
from evaluate.integration.command import load_run_configuration, models_match

from evaluate.transcripts.parser import parse_test_cases, suite_names
from evaluate.transcripts.transcript import (
    Document, place_held_sessions, requires_location_emulator, save_transcript,
)

PACKAGE = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PACKAGE / "configs" / "full.json"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Register transcript-collection options; shared by the package and `python -m evaluate` CLIs."""
    parser.add_argument("suites", nargs="+", help="sessions, sanity, or both (session is an alias)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, help="Run root; defaults to EVALUATION_EVIDENCE_ROOT or /var/tmp/studio-eval")
    parser.add_argument("--dry-run", action="store_true", help="Parse and count queries without starting services or calling APIs")
    parser.add_argument("--scenario", action="append", default=[],
                        help="Select a source or namespaced scenario ID within the chosen suites; repeatable")


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evaluate transcripts",
                                     description="Collect live conversation transcripts without scoring")
    add_arguments(parser)
    return parser.parse_args(argv)


def _prepare_environment(config: RunConfiguration, output_root: Path | None) -> Path:
    root = (output_root or Path(os.environ.get("EVALUATION_EVIDENCE_ROOT", "/var/tmp/studio-eval"))).resolve()
    directory = root / config.run_id
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evaluate.integration.settings")
    os.environ["EVALUATION_EVIDENCE_ROOT"] = str(root)
    os.environ["EVALUATION_EVIDENCE_DIR"] = str(directory)
    os.environ["EVALUATION_RUN_ID"] = config.run_id
    import django
    django.setup()
    return directory


def _verify_stack(config: RunConfiguration, directory: Path) -> bool:
    from django.conf import settings
    from evaluate.integration.preflight import run_preflight

    if evaluation_location_provider() not in {"emulator", "openstreetmap"}:
        raise ValueError("Evaluation location provider must be emulator or openstreetmap")
    if not models_match(settings, config):
        return False
    report = run_preflight(base_url=config.base_url, evidence_directory=directory)
    if not report["valid"]:
        print(json.dumps(report, indent=2), file=sys.stderr)
    return report["valid"]


def _report_collection(document: Document, output: Path) -> int:
    sessions = document["sessions"]
    queries = [row for session in sessions for row in session["queries"]]
    errors = sum(bool(session.get("errors")) or any(row.get("error") for row in session["queries"])
                 for session in sessions)
    print(json.dumps({"output": str(output), "sessions": len(sessions),
                      "skipped_sessions": sum(bool(session.get("skipped")) for session in sessions),
                      "queries": len(queries),
                      "executed_queries": sum(bool(row.get("executed")) for row in queries),
                      "expected_rejections": sum(bool(row.get("expected_rejection")) for row in queries),
                      "execution_errors": errors}, indent=2))
    return 1 if errors else 0


def _run_live(config: RunConfiguration, cases: list[NormalizedScenario], output_root: Path | None) -> int:
    if any(case.scenario_plan_version != config.scenario_plan_version for case in cases):
        raise ValueError("Configuration and dataset setup plan versions differ")
    config = config.model_copy(update={"run_id": str(uuid4())})
    directory = _prepare_environment(config, output_root)
    if not _verify_stack(config, directory):
        return 2
    from evaluate.integration.compose import build_components
    from evaluate.integration.telemetry import install_process_journal
    from evaluate.transcripts.runner import run_sessions

    directory.mkdir(parents=True, exist_ok=False)
    install_process_journal(directory, config.run_id)
    output = directory / "transcripts.json"
    held = {case.scenario_id for case in cases
            if evaluation_location_provider() == "openstreetmap" and requires_location_emulator(case)}
    live = [case for case in cases if case.scenario_id not in held]
    if held:
        names = ", ".join(case.source_id for case in cases if case.scenario_id in held)
        print(f"Skipping emulator-only geocoding sessions: {names}", file=sys.stderr, flush=True)
    print(f"Writing evidence and transcripts to {directory}", file=sys.stderr, flush=True)
    if live:
        document = run_sessions(config, live, build_components(config, directory), output)
    else:
        document = {"run_id": config.run_id, "sessions": []}
    if held:
        document = place_held_sessions(document, cases, held)
        save_transcript(output, document)
    return _report_collection(document, output)


def _execute(args: argparse.Namespace) -> int:
    selected = suite_names(args.suites)
    config, _ = load_run_configuration(args.config)
    sessions, sanity = parse_test_cases(config.dataset_directory)
    suites = {"sessions": sessions, "sanity": sanity}
    cases = [case for name in selected for case in suites[name]]
    if args.scenario:
        requested = set(args.scenario)
        available = {key for case in cases for key in (case.source_id, case.scenario_id)}
        if requested - available:
            raise ValueError("Unknown scenario or scenario outside the selected suites")
        cases = [case for case in cases if requested & {case.source_id, case.scenario_id}]
    if args.dry_run:
        print(json.dumps({
            "suites": selected, "sessions": len(cases),
            "queries": sum(len(case.turns) for case in cases),
            "emulator_only": [case.source_id for case in cases if requires_location_emulator(case)],
        }, indent=2))
        return 0
    return _run_live(config, cases, args.output)


def run(args: argparse.Namespace) -> int:
    """Collect unscored transcripts through the existing stack from parsed arguments."""
    try:
        return _execute(args)
    except KeyboardInterrupt:
        print("Interrupted; completed replies remain in transcripts.json and turns.jsonl.", file=sys.stderr)
        return 130
    except Exception as exc:
        from evaluate.evidence.crashes import safe_message
        print(safe_message(exc), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    """Parse selection and collect unscored transcripts through the existing stack."""
    return run(_arguments(argv))


if __name__ == "__main__":
    raise SystemExit(main())
