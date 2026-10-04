"""Wait for development readiness inside web; no dedicated evaluation ports.

Use scripts/evaluate-dev up first, then run this module via Compose exec web.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for development evaluation readiness")
    parser.add_argument("--output", type=Path, default=Path("/var/tmp/studio-eval"))
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "evaluate.integration.settings")
    import django
    django.setup()
    from evaluate.integration.preflight import run_preflight

    deadline = time.monotonic() + args.timeout
    while True:
        report = run_preflight(base_url=args.base_url, evidence_directory=args.output)
        if report["valid"] or time.monotonic() >= deadline:
            print(json.dumps(report, indent=2))
            return 0 if report["valid"] else 2
        time.sleep(min(2, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    raise SystemExit(main())
