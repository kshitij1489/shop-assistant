"""Durable evidence directory implementing the `EvidenceWriter` protocol.

Layout under the run directory (never inside the source tree):

    manifest.json      RunManifest                 summary.json   EvaluationSummary
    events.jsonl       ExecutionEvent journal      turns.jsonl    TurnEvidence journal
    snapshots.jsonl    StateSnapshot journal       assertions.jsonl  AssertionResult journal
    dispatch.jsonl     DispatchRecord ledger       attempts.jsonl    AttemptRecord ledger
    crashes/*.json     CrashRecord files           reports/*.json    LoadReport / BudgetReport
"""
from __future__ import annotations

import json
from pathlib import Path
import threading
from typing import Any

from pydantic import BaseModel

from evaluate.contracts.models import (
    AssertionResult, EvaluationSummary, ExecutionEvent, RunManifest, StateSnapshot, TurnEvidence,
)
from evaluate.evidence.journal import JournalContents, JournalError, JournalWriter, read_journal
from evaluate.evidence.records import AttemptRecord, CrashRecord, DispatchRecord, RunnerRecord
from evaluate.evidence.redaction import assert_redacted
from evaluate.identity import canonical_hash

Artifact = RunManifest | ExecutionEvent | TurnEvidence | StateSnapshot | AssertionResult | EvaluationSummary

JOURNALS: dict[type, str] = {
    ExecutionEvent: "events.jsonl", TurnEvidence: "turns.jsonl",
    StateSnapshot: "snapshots.jsonl", AssertionResult: "assertions.jsonl",
}
LEDGERS = ("dispatch.jsonl", "attempts.jsonl")
DOCUMENTS: dict[type, str] = {RunManifest: "manifest.json", EvaluationSummary: "summary.json"}
IDENTITY_FIELDS: dict[type, tuple[str, ...]] = {
    ExecutionEvent: ("event_id",), TurnEvidence: ("event_id", "request_id"),
    StateSnapshot: ("event_id", "snapshot_id"), AssertionResult: ("event_id", "assertion_id"),
}


class EvidenceConflict(ValueError):
    """Different content was written under an identifier that already exists."""


class EvidenceStore:
    """Validate, redact, deduplicate and durably append evaluation evidence."""

    def __init__(self, directory: Path, run_id: str) -> None:
        self.directory = Path(directory)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._index: dict[tuple[str, str], str] = {}
        self._attempts: dict[tuple[str, int], str] = {}
        (self.directory / "crashes").mkdir(parents=True, exist_ok=True)
        (self.directory / "reports").mkdir(parents=True, exist_ok=True)
        self.recovered: dict[str, JournalContents] = {}
        self._rebuild_index()  # read before writers repair any dangling final line
        self._journals = {name: JournalWriter(self.directory / name) for name in [*JOURNALS.values(), *LEDGERS]}

    # -- protocol -----------------------------------------------------------------
    def write(self, artifact: Artifact) -> None:
        kind = type(artifact)
        if kind not in JOURNALS and kind not in DOCUMENTS:
            raise TypeError("unsupported evidence artifact")
        payload = self._prepare(artifact)
        with self._lock:
            if kind in DOCUMENTS:
                self._write_document(kind, payload)
                return
            self._check_references(kind, payload)
            if self._register(kind, payload):
                self._journals[JOURNALS[kind]].append(payload)

    def flush(self) -> None:
        for journal in self._journals.values():
            journal.flush()

    def close(self) -> None:
        for journal in self._journals.values():
            journal.close()

    # -- runner-owned ledgers -------------------------------------------------------
    def record_dispatch(self, record: DispatchRecord) -> None:
        payload = self._prepare(record)
        self._journals["dispatch.jsonl"].append(payload)
        self._journals["dispatch.jsonl"].flush()

    def record_attempt(self, record: AttemptRecord) -> None:
        """Append the attempt decision. Identical replays are ignored."""
        payload = self._prepare(record)
        key = (record.scenario_instance_id, record.attempt)
        digest = canonical_hash(payload)
        with self._lock:
            previous = self._attempts.get(key)
            if previous == digest:
                return
            if previous is not None:
                raise EvidenceConflict("attempt decision already recorded with different content")
            self._attempts[key] = digest
            self._journals["attempts.jsonl"].append(payload)
            self._journals["attempts.jsonl"].flush()

    def attempt_records(self) -> list[AttemptRecord]:
        return [AttemptRecord.model_validate(row) for row in self.read("attempts.jsonl").records]

    def record_crash(self, record: CrashRecord) -> Path:
        # Crash text is masked by the caller; a second check guards against regressions.
        payload = self._prepare(record)
        path = self.directory / "crashes" / f"{record.occurred_at.replace(':', '')}-{record.crash_id}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def write_report(self, name: str, report: RunnerRecord) -> Path:
        payload = self._prepare(report)
        path = self.directory / "reports" / f"{name}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return path

    def read(self, name: str) -> JournalContents:
        return read_journal(self.directory / name)

    def dispatch_records(self) -> list[DispatchRecord]:
        return [DispatchRecord.model_validate(r) for r in self.read("dispatch.jsonl").records]

    # -- internals ---------------------------------------------------------------------
    def _prepare(self, model: BaseModel) -> dict[str, Any]:
        payload = model.model_dump(mode="json")
        if payload.get("run_id") not in (None, self.run_id):
            raise EvidenceConflict("artifact belongs to a different run")
        assert_redacted(payload)
        return payload

    def _register(self, kind: type, payload: dict[str, Any]) -> bool:
        """Return True when the payload is new; False when it is an identical duplicate."""
        digest = canonical_hash(payload)
        keys = [(field, payload[field]) for field in IDENTITY_FIELDS[kind]]
        existing = [self._index.get(key) for key in keys]
        if all(seen == digest for seen in existing) and existing[0] is not None:
            return False
        if any(seen is not None for seen in existing):
            raise EvidenceConflict(f"identifier reuse with different content in {JOURNALS[kind]}")
        for key in keys:
            self._index[key] = digest
        return True

    def _check_references(self, kind: type, payload: dict[str, Any]) -> None:
        """Snapshot and evidence ids must already have been written in this run."""
        if kind is TurnEvidence:
            missing = [sid for sid in payload["snapshot_ids"] if ("snapshot_id", sid) not in self._index]
            if missing:
                raise EvidenceConflict("turn evidence references an unknown snapshot")
        if kind is AssertionResult:
            known = {value for (field, value) in self._index if field in {"event_id", "snapshot_id", "assertion_id"}}
            missing = [item for item in payload["evidence_ids"] if item not in known]
            if missing:
                raise EvidenceConflict("assertion references unknown evidence")

    def _write_document(self, kind: type, payload: dict[str, Any]) -> None:
        path = self.directory / DOCUMENTS[kind]
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing == payload or (kind is RunManifest and _same_manifest(existing, payload)):
                return
            raise EvidenceConflict(f"{path.name} already exists with different content")
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    def _rebuild_index(self) -> None:
        for kind, name in JOURNALS.items():
            contents = read_journal(self.directory / name)
            self.recovered[name] = contents
            for payload in contents.records:
                try:
                    self._register(kind, payload)
                except (KeyError, EvidenceConflict) as exc:
                    raise JournalError(f"cannot resume from inconsistent {name}") from exc
        for name in LEDGERS:
            self.recovered[name] = read_journal(self.directory / name)
        for payload in self.recovered["attempts.jsonl"].records:
            key = (payload["scenario_instance_id"], payload["attempt"])
            digest = canonical_hash(payload)
            previous = self._attempts.get(key)
            if previous not in (None, digest):
                raise JournalError("cannot resume from inconsistent attempts.jsonl")
            self._attempts[key] = digest


def _same_manifest(existing: dict[str, Any], payload: dict[str, Any]) -> bool:
    """A rebuilt manifest differs only by its clock; keep the original bytes."""
    def stable(document: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in document.items() if key != "created_at"}
    return stable(existing) == stable(payload)
