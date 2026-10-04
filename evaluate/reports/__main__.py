"""Score saved evidence and regenerate reports. Does not send chat."""
import argparse
import json
from pathlib import Path
import sys
from uuid import uuid4

from .artifacts import load_artifacts, read_json
from .render import write_report
from .scoring import compare_runs, evaluate_run
from evaluate.judges import ManualJudge


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m evaluate.reports")
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser("score", help="Offline scoring from immutable saved evidence")
    score.add_argument("artifacts", type=Path)
    score.add_argument("--output", required=True, type=Path)
    score.add_argument("--manual-review", type=Path)
    score.add_argument("--compare", type=Path)
    score.add_argument("--scenarios", type=Path, help="Saved normalized scenarios or a normalized bundle")
    score.add_argument("--checks", type=Path, help="Reviewed structured assertions JSON")
    score.add_argument("--knowledge", type=Path, help="Saved knowledge evidence JSON")
    render = commands.add_parser("render", help="Regenerate reports from saved judgments without scoring")
    render.add_argument("report", type=Path)
    render.add_argument("--output", required=True, type=Path)
    compare = commands.add_parser("compare", help="Compare reports and flag incompatibilities")
    compare.add_argument("previous", type=Path)
    compare.add_argument("current", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            print(json.dumps(compare_runs(read_json(args.previous), read_json(args.current)), indent=2))
            return 0
        if args.command == "score":
            bundle = load_artifacts(args.artifacts, scenarios_path=args.scenarios, checks_path=args.checks, knowledge_path=args.knowledge)
            judge = ManualJudge(read_json(args.manual_review)) if args.manual_review else None
            report = evaluate_run(bundle, judge)
            if args.compare:
                report["comparison"] = compare_runs(read_json(args.compare), report)
        else:
            report = read_json(args.report)
            report["rendered_from_evaluation_id"] = report["evaluation_id"]
            report["evaluation_id"] = str(uuid4())
        target = write_report(report, args.output)
        print(json.dumps({"evaluation_id": report["evaluation_id"], "outcome": report["overall"]["outcome"], "directory": str(target)}))
        # Behavioral outcomes are report data, not an application/CLI crash.
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("Invalid, incompatible or inaccessible evaluation artifacts; no execution was attempted.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
