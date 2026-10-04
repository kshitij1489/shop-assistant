"""Default branch and settlement policies over the `evaluate-runner/v1` projection.

State inspectors that follow this convention expose, inside `StateSnapshot.state`:

    "chat": {"pending_question": "<documented ask text>" | null}
    or the website session shape, at the top level or under "chat":
        "ongoing_query_queue": [{"follow_up_question": ["How many ...?"]}],
        "awaiting_followup_index": 0
    "payment":  {"status": "pending" | "captured" | "failed" | "cancelled" | "none"}
    "payments": [{"status": ...}, ...]              # list form is accepted as well
    "pos":      {"status": "queued" | "submitted" | "accepted" | "failed" | "none"}
    "pending_async": ["payment", "pos"]             # optional explicit override

Sections listed in `unavailable_sections` are treated as unknown, never as settled
or matched. A projection that only hashes session state cannot show the open
question; the oracle returns "unknown" until the question text or the session
queue is present. Other projections can supply their own implementations.
"""
from __future__ import annotations

import json
import re
import unicodedata

from evaluate.contracts.models import NormalizedScenario, PendingTaskExpectation, ReferenceTurn, StateSnapshot
from evaluate.runner.ports import BranchState

PENDING_PAYMENT = frozenset({"pending", "created", "processing", "unknown"})
PENDING_POS = frozenset({"queued", "submitted", "pending", "unknown"})
ASYNC_SECTIONS = frozenset({"payment", "payments", "pos"})
# Strip punctuation/symbols; keep letters and numbers from every script.
_NORMALIZE = re.compile(r"[^\w\s]+", re.UNICODE)
_UNDERSCORE = re.compile(r"_+")


def normalize_question(text: str) -> str:
    """Casefold and drop punctuation/extra whitespace while preserving Unicode letters.

    Non-Latin questions (Hindi, etc.) must still match themselves. Underscores are
    treated as separators so identifier-like tokens do not glue adjacent words.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    cleaned = _NORMALIZE.sub(" ", folded)
    cleaned = _UNDERSCORE.sub(" ", cleaned)
    return " ".join(cleaned.split())


class SnapshotBranchOracle:
    """Verify that the projected queue provides the required clarification capability."""

    def pending_question(self, snapshot: StateSnapshot, reference: ReferenceTurn,
                         previous_reply: str | None) -> BranchState:
        if reference.asks is None or "chat" in snapshot.unavailable_sections:
            return "unknown"
        if reference.pending is not None:
            chat = snapshot.state.get('chat')
            queue = chat.get('ongoing_query_queue') if isinstance(chat, dict) else None
            if not isinstance(queue, list):
                return 'unknown'
            expected_tasks = [reference.pending, *reference.pending.equivalent_tasks]
            awaiting = chat.get('awaiting_followup_index')
            if awaiting is not None:
                if (isinstance(awaiting, bool) or not isinstance(awaiting, int)
                        or not 0 <= awaiting < len(queue)):
                    return 'unknown'
                # The queue retains suspended work. Only the row named by the
                # active pointer represents the question the next reply answers.
                return _pending_task_state(queue[awaiting], expected_tasks)
            if any(not isinstance(row, dict) or
                   type(row.get('is_complete')) is not bool or
                   any(not isinstance(row.get(key), str) or not row[key].strip()
                       for key in ('intent_type', 'sub_intent')) for row in queue):
                return 'unknown'
            # With no active pointer, a unique suspended task can still establish
            # the capability across a pause. Multiple candidates are ambiguous.
            matches = [row for row in queue if row['is_complete'] is False and any(
                       row['intent_type'] == expected.intent_type
                       and row['sub_intent'] in expected.sub_intents
                       for expected in expected_tasks)]
            if len(matches) > 1:
                return 'unknown'
            if matches:
                return _pending_task_state(matches[0], expected_tasks)
            return 'mismatch'
        pending = _open_question(snapshot.state)
        if pending is _MISSING:
            return "unknown"
        if pending is None or not isinstance(pending, str):
            return "mismatch" if pending is None else "unknown"
        if normalize_question(pending) == normalize_question(reference.asks):
            return "matched"
        # Legacy references without typed expectations can establish an exact
        # match, but different wording is unknown rather than a guessed mismatch.
        return "unknown"


_MISSING = object()


def _pending_task_state(task: object, expected_tasks: list[PendingTaskExpectation]) -> BranchState:
    """Judge one active/suspended task without consulting unrelated queue rows."""
    if (not isinstance(task, dict) or type(task.get('is_complete')) is not bool
            or any(not isinstance(task.get(key), str) or not task[key].strip()
                   for key in ('intent_type', 'sub_intent'))):
        return 'unknown'
    if task['is_complete'] or not any(
            task['intent_type'] == expected.intent_type
            and task['sub_intent'] in expected.sub_intents
            for expected in expected_tasks):
        return 'mismatch'
    identity, questions = task.get('query_id'), task.get('follow_up_question')
    if not (type(identity) is int and identity >= 0
            or isinstance(identity, str) and identity.strip()):
        return 'unknown'
    if not isinstance(questions, list) or any(not isinstance(q, str) for q in questions):
        return 'unknown'
    if not questions or not questions[-1].strip():
        return 'mismatch'
    return 'matched'


def _open_question(state: dict) -> object:
    """Return the open question, None when the projection says nothing is open, or _MISSING."""
    chat = state.get("chat")
    if isinstance(chat, dict) and "pending_question" in chat:
        return chat.get("pending_question")
    for source in (chat, state):
        if isinstance(source, dict) and ({"ongoing_query_queue", "pending_queries", "awaiting_followup_index"} & set(source)):
            return _question_from_queue(source)
    return _MISSING


def _question_from_queue(source: dict) -> object:
    """Read the website session queue. An empty queue means no question is open."""
    index = source.get("awaiting_followup_index")
    queue = source.get("ongoing_query_queue", source.get("pending_queries", []))
    if not isinstance(queue, list) or index is None or not queue:
        return None
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(queue):
        return _MISSING
    item = queue[index]
    if not isinstance(item, dict):
        return _MISSING
    questions = item.get("follow_up_question")
    if isinstance(questions, str):
        return questions
    if isinstance(questions, list) and questions and isinstance(questions[-1], str):
        return questions[-1]
    return _MISSING


def later_payment_control_expected(scenario: NormalizedScenario, original_turn_index: int | None) -> bool:
    """True when a later `payment_control` action will settle payment/POS state.

    Pending payment after a turn that only creates the charge is expected when the
    scenario still has a capture/fail/cancel (or any payment_control) ahead.
    """
    for action in scenario.actions:
        if getattr(action.operation, "kind", None) != "payment_control":
            continue
        action_index = action.original_turn_index
        if action_index is None:
            continue  # setup-time controls already ran
        if original_turn_index is None or action_index > original_turn_index:
            return True
    return False


class ProjectionSettlementPolicy:
    """Payment/POS sections are pending until they reach a terminal status."""

    def pending_sections(self, snapshot: StateSnapshot) -> list[str]:
        explicit = snapshot.state.get("pending_async")
        if isinstance(explicit, list):
            return [str(name) for name in explicit]
        pending = []
        for section, statuses in (("payment", PENDING_PAYMENT), ("payments", PENDING_PAYMENT), ("pos", PENDING_POS)):
            if section in snapshot.unavailable_sections:
                continue
            if any(status in statuses for status in _statuses(snapshot.state.get(section))):
                pending.append(section)
        return pending

    def unexpected_pending_sections(self, snapshot: StateSnapshot, scenario: NormalizedScenario | None,
                                    original_turn_index: int | None) -> list[str]:
        """Pending async work that this turn is responsible for settling.

        When a later payment_control remains in the scenario, payment/POS pending
        is expected and must not time out the attempt.
        """
        pending = self.pending_sections(snapshot)
        if scenario is None:
            return pending
        terminal_requested = any(
            a.operation.kind == "payment_control"
            and a.operation.operation in {"capture", "fail", "cancel"}
            and (a.original_turn_index is None or
                 (original_turn_index is not None and a.original_turn_index <= original_turn_index))
            for a in scenario.actions)
        if not terminal_requested:
            # Creating a checkout link is not a request to pay it. POS work, when
            # queued, remains independently awaited even if payment is unpaid.
            pending = [name for name in pending if name not in {"payment", "payments"}]
        # Unpaid is a customer state; pending provider work is an asynchronous
        # operation. Observe creation before the next turn even when capture is
        # deferred. Unknown/failed commands require recovery, not endless polling.
        commands = snapshot.state.get('commands', [])
        if isinstance(commands, list) and any(
                isinstance(command, dict) and command.get('kind') == 'payment.create'
                and command.get('status') in {'pending', 'leased'} for command in commands):
            if 'payment_creation' not in pending:
                pending.append('payment_creation')
        return pending

    def fingerprint(self, snapshot: StateSnapshot) -> str:
        parts = {name: snapshot.state.get(name) for name in ("payment", "payments", "pos", "order", "orders", "commands", "pending_async")}
        return json.dumps(parts, sort_keys=True, default=str)


def _statuses(section: object) -> list[str]:
    """Status strings from either `{"status": ...}` or a list of such objects."""
    entries = section if isinstance(section, list) else [section]
    return [entry["status"] for entry in entries
            if isinstance(entry, dict) and isinstance(entry.get("status"), str)]
