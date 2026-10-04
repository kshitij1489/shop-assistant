"""Read-only adapter for a bundle or the execution contract's JSON/JSONL files."""
import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from evaluate.checks.models import CheckSpec
from evaluate.contracts.models import (StrictModel, RunManifest, NormalizedScenario,
                                       TurnEvidence, StateSnapshot, ExecutionEvent, ExecutionIdentity, JsonValue)


class Knowledge(StrictModel):
    evidence_id: str
    source: str
    content: JsonValue


class Diagnostic(StrictModel):
    evidence_id: str
    kind: Literal["crash", "dispatch", "attempt", "runner_report", "integrity", "application"]
    source: str
    data: dict[str, JsonValue]


class Measurement(StrictModel):
    request_id: str
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    currency: str | None = None
    api_errors: int | None = Field(default=None, ge=0)


class RunArtifacts(StrictModel):
    evaluation_input_version: Literal["1.0.0"] = "1.0.0"
    manifest: RunManifest
    scenarios: list[NormalizedScenario]
    turns: list[TurnEvidence] = Field(default_factory=list)
    snapshots: list[StateSnapshot] = Field(default_factory=list)
    events: list[ExecutionEvent] = Field(default_factory=list)
    instances: list[ExecutionIdentity] = Field(default_factory=list)
    knowledge: list[Knowledge] = Field(default_factory=list)
    checks: list[CheckSpec] = Field(default_factory=list)
    measurements: list[Measurement] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        def unique(values, label):
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label}")
        unique([s.scenario_id for s in self.scenarios], "scenario")
        scenarios = {s.scenario_id: s for s in self.scenarios}
        if set(self.manifest.scenario_ids) != set(scenarios):
            raise ValueError("Saved scenarios must match manifest selection")
        unique([s.check_id for s in self.checks], "check")
        unique([k.evidence_id for k in self.knowledge], "knowledge evidence")
        unique([t.request_id for t in self.turns], "request")
        unique([m.request_id for m in self.measurements], "measurement request")
        unique([s.snapshot_id for s in self.snapshots], "snapshot")
        all_records = [*self.turns, *self.snapshots, *self.events]
        unique([r.event_id for r in all_records], "event")
        unique([r.event_id for r in all_records] + [s.snapshot_id for s in self.snapshots]
               + [d.evidence_id for d in self.diagnostics]
               + [k.evidence_id for k in self.knowledge] + [f"scenario:{s}" for s in scenarios], "evidence identity")
        ownership = {}
        for row in [*all_records, *self.instances]:
            if row.run_id != self.manifest.run_id or row.scenario_id not in scenarios:
                raise ValueError("Artifact does not belong to this run/selection")
            key = row.scenario_instance_id
            if key in ownership and ownership[key] != row.scenario_id:
                raise ValueError("Instance reused across scenarios")
            ownership[key] = row.scenario_id
        turns = {t.request_id: t for t in self.turns}
        snapshots = {s.snapshot_id: s for s in self.snapshots}
        for turn in self.turns:
            specs = {t.original_turn_index: t for t in scenarios[turn.scenario_id].turns}
            source = specs.get(turn.original_turn_index)
            if source is None or source.user_turn_index != turn.user_turn_index:
                raise ValueError("Saved turn does not match normalized input")
            for snapshot_id in turn.snapshot_ids:
                snap = snapshots.get(snapshot_id)
                if snap and ((snap.scenario_instance_id, snap.attempt, snap.original_turn_index) != (
                        turn.scenario_instance_id, turn.attempt, turn.original_turn_index) or
                        snap.request_id != turn.request_id and not (snap.phase == "before" and snap.request_id is None)):
                    raise ValueError("Turn references another execution's state")
        for row in [*self.snapshots, *self.events]:
            turn = turns.get(row.request_id)
            if turn and (row.scenario_instance_id, row.attempt, row.original_turn_index) != (
                    turn.scenario_instance_id, turn.attempt, turn.original_turn_index):
                raise ValueError("Request references another execution")
        for check in self.checks:
            if check.scenario_id not in scenarios or check.original_turn_index not in {
                    t.original_turn_index for t in scenarios[check.scenario_id].turns}:
                raise ValueError("Check does not target a saved scenario turn")
        if any(m.request_id not in turns for m in self.measurements):
            raise ValueError("Measurement references an unknown request")
        if any(d.data.get("run_id", self.manifest.run_id) != self.manifest.run_id for d in self.diagnostics):
            raise ValueError("Diagnostic belongs to another run")
        for diagnostic in self.diagnostics:
            sid = diagnostic.data.get("scenario_id")
            instance = diagnostic.data.get("scenario_instance_id")
            if sid is not None and sid not in scenarios:
                raise ValueError("Diagnostic references an unselected scenario")
            if instance in ownership and sid != ownership[instance]:
                raise ValueError("Diagnostic references another scenario's instance")
        return self


def decode_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON number")))


def read_json(path):
    return decode_json(Path(path).read_text(encoding="utf-8"))


_TELEMETRY_KEYS = ("call_id", "event_id", "injected", "model", "provider_request_id", "error_type", "cache", "hit",
                   "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
                   "event", "status", "operation", "elapsed_ms")


def _application_measurements(data: dict) -> None:
    """Pair calls across journals and count each provider call exactly once."""
    known = {t["request_id"] for t in data.get("turns", [])}
    calls = {}
    for diagnostic in data.get("diagnostics", []):
        row = diagnostic.get("data", {})
        if diagnostic.get("kind") != "application" or row.get("event") not in {"llm.started", "llm.completed"}:
            continue
        request_id, call_id = row.get("request_id"), row.get("call_id")
        if request_id not in known or not call_id:
            continue
        calls.setdefault(request_id, {}).setdefault(call_id, {})[row["event"]] = row
    measurements = {m["request_id"]: m for m in data.get("measurements", [])}
    for request_id, request_calls in calls.items():
        complete = all("llm.started" in c and "llm.completed" in c for c in request_calls.values())
        rows = [c["llm.completed"] for c in request_calls.values() if "llm.completed" in c]
        measurement = {"request_id": request_id, "api_errors": None}
        if complete:
            measurement["api_errors"] = sum(r.get("status") == "failed" and not r.get("injected") for r in rows)
        for field in ("input_tokens", "output_tokens"):
            if complete and all(type(r.get(field)) is int and r[field] >= 0 for r in rows):
                measurement[field] = sum(r[field] for r in rows)
        measurements[request_id] = measurement
    data["measurements"] = list(measurements.values())


def _ingest_application_rows(data: dict, rows: list[dict], source: str) -> None:
    known = {t["request_id"] for t in data.get("turns", []) if isinstance(t, dict) and "request_id" in t}
    seen = {d["data"].get("event_id"): d["data"] for d in data.get("diagnostics", [])
            if d.get("kind") == "application" and d["data"].get("event_id")}
    for row in rows:
        event_id = row.get("event_id") or f"line-{len(data.get('diagnostics', []))}"
        payload = {key: row[key] for key in _TELEMETRY_KEYS if key in row and row[key] is not None}
        for key in ("run_id", "scenario_id", "scenario_instance_id", "attempt", "request_id"):
            if key in row and row[key] is not None:
                payload[key] = row[key]
        if event_id in seen:
            if seen[event_id] != payload:
                data.setdefault("diagnostics", []).append({
                    "evidence_id": f"integrity:{source}:{event_id}:{len(data['diagnostics'])}",
                    "kind": "integrity", "source": source, "data": {"reason": "Conflicting telemetry event ID"}})
            continue
        seen[event_id] = payload
        data.setdefault("diagnostics", []).append({
            "evidence_id": f"application:{source}:{event_id}",
            "kind": "application", "source": source, "data": payload})
    _application_measurements(data)


def _attach_generated_checks(data: dict) -> None:
    from evaluate.checks.plan_checks import generate_check_specs
    scenarios = [NormalizedScenario.model_validate(row) if isinstance(row, dict) else row
                 for row in data.get("scenarios", [])]
    generated = generate_check_specs(scenarios)
    if generated:
        data["checks"] = [spec.model_dump(mode="json") for spec in generated]


def load_artifacts(path, *, scenarios_path=None, checks_path=None, knowledge_path=None):
    path = Path(path)
    checks_supplied = checks_path is not None
    if path.is_file():
        data = read_json(path)
        checks_supplied = checks_supplied or "checks" in data
    elif (path / "artifacts.json").is_file():
        data = read_json(path / "artifacts.json")
        checks_supplied = checks_supplied or "checks" in data
    else:
        manifest = path / "manifest.json" if (path / "manifest.json").is_file() else path / "run_manifest.json"
        data = {"manifest": read_json(manifest), "scenarios": read_json(scenarios_path or path / "scenarios.json"), "diagnostics": []}

        def journal(file):
            records = []
            for number, line in enumerate(file.read_bytes().splitlines(keepends=True), 1):
                if not line.strip():
                    continue
                try:
                    row = decode_json(line.decode("utf-8"))
                    if not isinstance(row, dict):
                        raise ValueError("Expected object")
                    records.append(row)
                    if not line.endswith(b"\n"):
                        raise ValueError("Unterminated journal record")
                except (ValueError, UnicodeError):
                    data["diagnostics"].append({"evidence_id": f"integrity:{file.name}:{number}", "kind": "integrity",
                                                "source": file.name, "data": {"line": number, "problem": "Damaged or unterminated journal record"}})
            return records

        for name in ("turns", "snapshots", "events", "instances", "knowledge", "checks", "measurements", "diagnostics"):
            if (path / f"{name}.json").is_file():
                if name == "diagnostics":
                    data[name].extend(read_json(path / f"{name}.json"))
                else:
                    data[name] = read_json(path / f"{name}.json")
                if name == "checks":
                    checks_supplied = True
            elif (path / f"{name}.jsonl").is_file():
                data[name] = journal(path / f"{name}.jsonl")
                if name == "checks":
                    checks_supplied = True
        if (path / "dispatch.jsonl").is_file():
            for n, row in enumerate(journal(path / "dispatch.jsonl")):
                data["diagnostics"].append({"evidence_id": f"dispatch:{n}", "kind": "dispatch", "source": "dispatch.jsonl", "data": row})
        if (path / "attempts.jsonl").is_file():
            for n, row in enumerate(journal(path / "attempts.jsonl")):
                data["diagnostics"].append({"evidence_id": f"attempt:{n}", "kind": "attempt", "source": "attempts.jsonl", "data": row})
        for dirname, kind in (("crashes", "crash"), ("reports", "runner_report")):
            for file in sorted((path / dirname).glob("*.json")):
                data["diagnostics"].append({"evidence_id": f"{kind}:{file.name}", "kind": kind,
                                            "source": f"{dirname}/{file.name}", "data": read_json(file)})
        for file in sorted(path.glob("application-*.jsonl")):
            _ingest_application_rows(data, journal(file), file.name)
    for key, override in (("scenarios", scenarios_path), ("checks", checks_path), ("knowledge", knowledge_path)):
        if override is not None:
            data[key] = read_json(override)
    if isinstance(data["scenarios"], dict):
        data["scenarios"] = data["scenarios"]["scenarios"]
    if scenarios_path is not None:
        data["scenarios"] = [s for s in data["scenarios"] if s["scenario_id"] in data["manifest"]["scenario_ids"]]
    if not checks_supplied and not data.get("checks"):
        _attach_generated_checks(data)
    return RunArtifacts.model_validate(data)
