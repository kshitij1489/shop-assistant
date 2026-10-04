"""Project framework evidence into the documented transcript JSON contract."""
import json
import os
from pathlib import Path
from typing import Any, TypeAlias

from evaluate.contracts.models import NormalizedScenario, Turn
from evaluate.evidence.journal import read_journal
from evaluate.evidence.redaction import redact_value

Document: TypeAlias = dict[str, Any]
EMULATOR_HOLD = (
    "Synthetic geocoding faults require the location emulator. "
    "This live OpenStreetMap transcript did not send the conversation."
)
_SYNTHETIC_GEOCODING = frozenset({"geocoding", "reverse_geocoding"})


def requires_location_emulator(case: NormalizedScenario) -> bool:
    """Return whether a reviewed action forces a geocoding result live OpenStreetMap cannot produce."""
    for action in case.actions:
        operation = action.operation
        if getattr(operation, "kind", None) != "lookup_control":
            continue
        if getattr(operation, "service", None) not in _SYNTHETIC_GEOCODING:
            continue
        if getattr(operation, "outcome", None) != "success":
            return True
    return False


def save_transcript(path: Path, document: Document) -> None:
    """Atomically replace the transcript without exposing partially written JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(redact_value(document), stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _query_row(turn: Turn) -> Document:
    return {
        "query": turn.text, "llm_answer": None,
        "is_followup": turn.turn_kind in {"followup", "answer_and_new_query", "prior_question_reply"},
        "turn_kind": turn.turn_kind, "executed": False,
    }


def _session_row(number: int, case: NormalizedScenario) -> Document:
    return {
        "session_id": f"session_{number}", "source_id": case.source_id,
        "suite": "sessions" if case.namespace == "sessions" else "sanity",
        "queries": [_query_row(turn) for turn in case.turns],
    }


def _records(directory: Path, name: str, run_id: str) -> list[Document]:
    contents = read_journal(directory / name)
    return [row for row in contents.records if row.get("run_id") == run_id]


def _reply_fields(evidence: Document, dispatch: Document) -> Document:
    status = dispatch.get("status", "completed" if evidence["http_status"] is not None else "in_flight_unknown")
    fields = {
        "llm_answer": evidence["response_text"], "http_status": evidence["http_status"],
        "executed": status == "completed", "dispatch_state": status,
    }
    if evidence.get("sent_message") is not None:
        fields["sent_query"] = evidence["sent_message"]
    return fields


def _record_reply(row: Document, evidence: Document, dispatch: Document, *, allow_empty_rejection: bool = False) -> None:
    row.update(_reply_fields(evidence, dispatch))
    row.pop("error", None)  # A successful safe retry replaces the previous transport error.
    row.pop("expected_rejection", None)
    row.pop("response_error", None)
    if evidence.get("response_error") is not None:
        row["response_error"] = evidence["response_error"]
    error = evidence.get("transport_error")
    if (allow_empty_rejection and row["executed"] and error is None
            and evidence["http_status"] == 400 and evidence.get("response_error") == "Missing message"):
        row["expected_rejection"] = "empty_input"
        return
    if error is None and not (evidence["http_status"] == 200 and evidence["response_text"] is not None):
        error = f"HTTP {evidence['http_status']}: no successful chat reply"
    if error:
        row["error"] = error


def _project_replies(run_id: str, cases: list[NormalizedScenario], sessions: list[Document], directory: Path) -> None:
    queries = {(case.scenario_id, turn.original_turn_index): row
               for case, session in zip(cases, sessions)
               for turn, row in zip(case.turns, session["queries"])}
    empty_rejections = {(case.scenario_id, turn.original_turn_index)
                        for case in cases for turn in case.turns if turn.allow_empty_rejection}
    dispatches = {row["request_id"]: row for row in _records(directory, "dispatch.jsonl", run_id)}
    for evidence in _records(directory, "turns.jsonl", run_id):
        key = (evidence["scenario_id"], evidence["original_turn_index"])
        row = queries.get(key)
        if row is not None:
            _record_reply(row, evidence, dispatches.get(evidence["request_id"], {}),
                          allow_empty_rejection=key in empty_rejections)


def _project_errors(run_id: str, sessions: dict[str, Document], directory: Path) -> None:
    for attempt in _records(directory, "attempts.jsonl", run_id):
        session = sessions.get(attempt["scenario_id"])
        if session is not None and attempt["failure"] != "none":
            session.setdefault("errors", []).append({"phase": attempt["failure"], "message": attempt["detail"]})
    for event in _records(directory, "events.jsonl", run_id):
        session = sessions.get(event["scenario_id"])
        if session is not None and event["kind"] in {"cleanup", "error"} and event["status"] == "failed":
            session.setdefault("errors", []).append({"phase": event["kind"], "message": event["detail"]})


def place_held_sessions(document: Document, cases: list[NormalizedScenario], held: set[str]) -> Document:
    """Put emulator-only sessions back in source order without treating the hold as an execution error."""
    present = {(row["suite"], row["source_id"]): row for row in document["sessions"]}
    ordered: list[Document] = []
    for case in cases:
        if case.scenario_id in held:
            row = _session_row(0, case)
            row["skipped"] = EMULATOR_HOLD
            ordered.append(row)
            continue
        suite = "sessions" if case.namespace == "sessions" else "sanity"
        ordered.append(present[(suite, case.source_id)])
    for number, row in enumerate(ordered, 1):
        row["session_id"] = f"session_{number}"
    return {**document, "sessions": ordered}


def export_transcript(run_id: str, cases: list[NormalizedScenario], output: Path) -> Document:
    """Export all selected queries, using the last dispatch of each logical turn.

    The consumer contract is ``sessions[].queries[]``; session/source identifiers
    stay alongside each query list. Unexecuted queries retain null replies. The
    native journals retain every safe retry and its request identity.
    """
    sessions = [_session_row(number, case) for number, case in enumerate(cases, 1)]
    _project_replies(run_id, cases, sessions, output.parent)
    _project_errors(run_id, {case.scenario_id: session for case, session in zip(cases, sessions)}, output.parent)
    document = {"run_id": run_id, "sessions": sessions}
    save_transcript(output, document)
    return document
