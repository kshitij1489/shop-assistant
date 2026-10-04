"""Build persistable crash diagnostics from exceptions without leaking credentials."""
from __future__ import annotations

from datetime import datetime, timezone
import traceback
from uuid import uuid4

from evaluate.contracts.models import ExecutionIdentity
from evaluate.evidence.records import CrashRecord, Phase
from evaluate.evidence.redaction import redact_text

MAX_FRAMES = 40


def safe_message(exc: BaseException, limit: int = 500) -> str:
    """Exception text with secrets masked and length bounded."""
    return redact_text(str(exc))[:limit] or exc.__class__.__name__


def build_crash_record(exc: BaseException, phase: Phase, run_id: str,
                       identity: ExecutionIdentity | None = None,
                       original_turn_index: int | None = None, user_turn_index: int | None = None,
                       request_id: str | None = None, related_event_ids: list[str] | None = None,
                       related_files: list[str] | None = None) -> CrashRecord:
    frames = traceback.format_exception(type(exc), exc, exc.__traceback__)
    redacted_frames = [redact_text(frame.rstrip("\n")) for frame in frames][-MAX_FRAMES:]
    return CrashRecord(
        crash_id=f"crash-{uuid4().hex}", run_id=run_id,
        scenario_id=identity.scenario_id if identity else None,
        scenario_instance_id=identity.scenario_instance_id if identity else None,
        attempt=identity.attempt if identity else None,
        original_turn_index=original_turn_index, user_turn_index=user_turn_index,
        request_id=request_id, phase=phase, exception_type=f"{type(exc).__module__}.{type(exc).__qualname__}",
        message=safe_message(exc), traceback=redacted_frames,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        related_event_ids=list(related_event_ids or []), related_files=list(related_files or []),
    )
