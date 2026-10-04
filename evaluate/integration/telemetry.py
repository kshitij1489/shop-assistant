"""One append-only application journal per process. Callers choose the directory."""
from __future__ import annotations

import logging
import os
from pathlib import Path

from evaluate.controls.telemetry import EvidenceHandler

_HANDLER: EvidenceHandler | None = None


def install_process_journal(directory: Path, run_id: str) -> EvidenceHandler:
    """Attach one fsync handler. A second call for the same run returns the first."""
    global _HANDLER
    from evaluate.controls.telemetry import RoutedEvidenceHandler
    for existing in logging.getLogger("evaluate.telemetry").handlers:
        if isinstance(existing, RoutedEvidenceHandler):
            return existing
    if _HANDLER is not None:
        if _HANDLER.run_id == run_id:
            return _HANDLER
        logging.getLogger("evaluate.telemetry").removeHandler(_HANDLER)
        _HANDLER.close()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"application-{os.getpid()}.jsonl"
    handler = EvidenceHandler(path, run_id)
    logger = logging.getLogger("evaluate.telemetry")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    _HANDLER = handler
    return handler
