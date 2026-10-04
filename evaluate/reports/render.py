"""Self-contained HTML, JSON and CSV. Evidence is always escaped as text."""
import csv
import hashlib
import html
import io
import json
from pathlib import Path
import re

from evaluate.checks.models import OUTCOMES


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def escape(value):
    return html.escape(str(value), quote=True)


def anchor(value):
    return "e-" + hashlib.sha256(value.encode()).hexdigest()


def csv_text(rows, fields):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for name in fields:
            value = row.get(name)
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=False)
            # Keep spreadsheet software from evaluating arbitrary assistant text.
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                value = "'" + value
            safe[name] = value
        writer.writerow(safe)
    return output.getvalue()


def html_report(report):
    parts = ['<!doctype html><html lang="en"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'">',
             '<title>Saved run evaluation</title><style>body{font:15px system-ui;margin:2rem;color:#172033;background:#fafbfc}',
             'table{border-collapse:collapse;width:100%;margin:1rem 0}th,td{border:1px solid #cad1db;padding:.5rem;text-align:left;vertical-align:top}',
             'pre{white-space:pre-wrap;overflow-wrap:anywhere}input{padding:.7rem;width:90%}.FAIL{color:#a00020}.PASS{color:#17602e}',
             '.BLOCKED,.NEEDS_REVIEW{color:#805000}details{margin:.7rem 0}a{color:#164a9c}caption{text-align:left;font-weight:bold}</style>',
             '<body><h1>Saved run evaluation</h1>',
             f'<p>Run <b>{escape(report["run_id"])}</b> · evaluation {escape(report["evaluation_id"])} · {escape(report["created_at"])}</p>',
             '<p>PASS, FAIL, BLOCKED and NEEDS_REVIEW use all planned units as their denominator. Retries remain separate. No conversations were replayed.</p>']
    metrics = report['workload_metrics']
    if 'recorded_turns' in metrics:
        parts.append(f'<p>Recorded user turns: {escape(metrics["recorded_turns"])} / {escape(metrics["planned_turns"])} planned. '
                     f'Manual judgments pending: {escape(metrics["manual_judgments_pending"])}. '
                     'Unreviewed assertions prevent a session PASS; they are not observed failures.</p>')
    parts.append('<table><caption>Overall results</caption><tr><th>Unit</th><th>Denominator</th>' + ''.join(f'<th>{s}</th>' for s in OUTCOMES) + '<th>Decided denominator</th></tr>')
    for unit in ("assertions", "turns", "sessions"):
        summary = report["overall"][unit]
        parts.append(f'<tr><td>{unit}</td><td>{escape(summary["denominator"])}</td>' + ''.join(f'<td>{escape(summary["counts"][s])}</td>' for s in OUTCOMES) + f'<td>{escape(summary["decided_denominator"])}</td></tr>')
    parts.append('</table><h2>Coverage</h2>')
    for dimension, groups in report["coverage"].items():
        parts.append(f'<table><caption>{escape(dimension)}</caption><tr><th>Group</th><th>Unit / denominator</th>' + ''.join(f'<th>{s}</th>' for s in OUTCOMES) + '</tr>')
        for group, summary in groups.items():
            parts.append(f'<tr><td>{escape(group)}</td><td>{escape(summary["unit"])} / {escape(summary["denominator"])}</td>' + ''.join(f'<td>{escape(summary["counts"][s])}</td>' for s in OUTCOMES) + '</tr>')
        parts.append('</table>')
    for title, value in (("Workload measurements", report["workload_metrics"]), ("Judge accounting (separate from workload)", report["judge"]),
                         ("Provenance", report["provenance"])):
        parts.append(f'<details><summary>{escape(title)}</summary><pre>{escape(dumps(value))}</pre></details>')
    if "comparison" in report:
        parts.append('<h2>Run comparison</h2><pre>' + escape(dumps(report["comparison"])) + '</pre>')
    for title, key in (("Session results", "sessions"), ("Turn results", "turns")):
        parts.append(f'<details><summary>{title}</summary><pre>{escape(dumps(report[key]))}</pre></details>')
    parts.append('<h2>Assertions</h2><label>Search assertions and saved evidence <input id="search" type="search" placeholder="Scenario, verdict, criterion or evidence text"></label>')
    parts.append('<table><tr><th>Outcome</th><th>Scenario / attempt / turn</th><th>Criterion</th><th>Reason and evidence</th></tr>')
    for row in report["assertions"]:
        # Failure links include input/output, snapshots, logs and crash events
        # for this execution, even when they are not the assertion's citations.
        related = [key for key, record in report["evidence"].items() if isinstance(record, dict)
                   and record.get("scenario_instance_id") == row["scenario_instance_id"]
                   and record.get("attempt") == row["attempt"]
                   and (row["request_id"] is None or record.get("request_id") in {None, row["request_id"]})]
        links = ''.join(f'<li><a href="#{anchor(key)}">{escape(key)}</a>{" (cited)" if key in row["evidence_ids"] else ""}</li>' for key in dict.fromkeys(row["evidence_ids"] + related) if key in report["evidence"])
        parts.append(f'<tr class="searchable"><td class="{escape(row["outcome"])}">{escape(row["outcome"])}</td>'
                     f'<td>{escape(row["scenario_id"])} / {escape(row["attempt"])} / {escape(row["original_turn_index"])}</td>'
                     f'<td>{escape(row["criterion"])}</td><td>{escape(row["explanation"])}<ul>{links}</ul></td></tr>')
    parts.append('</table><h2>Saved evidence, logs and crash details</h2>')
    for key, value in report["evidence"].items():
        parts.append(f'<details class="searchable" id="{anchor(key)}"><summary>{escape(key)}</summary><pre>{escape(dumps(value))}</pre></details>')
    parts.append("""<script>
document.getElementById('search').addEventListener('input', function () {
  const query = this.value.toLocaleLowerCase();
  document.querySelectorAll('.searchable').forEach(function (node) {
    node.hidden = !node.textContent.toLocaleLowerCase().includes(query);
  });
});
function reveal() {
  const node = document.getElementById(location.hash.slice(1));
  if (node) { node.hidden = false; node.open = true; node.scrollIntoView(); }
}
window.addEventListener('hashchange', reveal); reveal();
</script></body></html>""")
    return ''.join(parts)


def write_report(report, output_root):
    """Exclusive directory allocation: neither old judgments nor evidence change."""
    ident = report["evaluation_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", ident):
        raise ValueError("Invalid evaluation ID")
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / ident
    target.mkdir(exist_ok=False)
    outputs = {"report.json": dumps(report), "report.html": html_report(report),
               "manual_review.json": dumps(report.get("manual_review", []))}
    for name in ("assertions", "turns", "sessions"):
        fields = list(report[name][0]) if report[name] else ["outcome"]
        outputs[f"{name}.csv"] = csv_text(report[name], fields)
    summaries = [{"unit": unit, "denominator": report["overall"][unit]["denominator"],
                  **report["overall"][unit]["counts"]} for unit in ("assertions", "turns", "sessions")]
    outputs["overall.csv"] = csv_text(summaries, ["unit", "denominator", *OUTCOMES])
    coverage = [{"dimension": dimension, "group": group, "unit": row["unit"],
                 "denominator": row["denominator"], **row["counts"]}
                for dimension, groups in report["coverage"].items() for group, row in groups.items()]
    outputs["coverage.csv"] = csv_text(coverage, ["dimension", "group", "unit", "denominator", *OUTCOMES])
    for name, content in outputs.items():
        with (target / name).open("x", encoding="utf-8", newline="") as stream:
            stream.write(content)
    return target
