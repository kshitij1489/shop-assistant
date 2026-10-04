"""Append-only JSON Lines journals with concurrency-safe writes and tolerant reads."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import threading
from typing import Any, Iterator


class JournalError(OSError):
    """Durable append or flush failed; execution must stop rather than drop evidence."""


def encode_record(record: dict[str, Any]) -> bytes:
    """Serialize one record as a single newline-terminated JSON line."""
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    return (text + "\n").encode("utf-8")


class JournalWriter:
    """Serialize appends from many threads; each record is one `write` call.

    A crash between two writes leaves at most one truncated final line, which
    `read_journal` tolerates. A crash mid-write never corrupts earlier records.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.repaired_truncated_tail = self._terminate_dangling_line()
        self._file = open(self.path, "ab", buffering=0)

    def _terminate_dangling_line(self) -> bool:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return False
        with open(self.path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            dangling = handle.read(1) != b"\n"
        if dangling:
            # Start the next record on its own line; readers report the damaged one.
            with open(self.path, "ab") as handle:
                handle.write(b"\n")
        return dangling

    def append(self, record: dict[str, Any]) -> None:
        payload = encode_record(record)
        with self._lock:
            try:
                self._file.write(payload)
            except OSError as exc:
                raise JournalError(f"append failed: {self.path.name}") from exc

    def flush(self) -> None:
        with self._lock:
            try:
                self._file.flush()
                os.fsync(self._file.fileno())
            except OSError as exc:
                raise JournalError(f"flush failed: {self.path.name}") from exc

    def close(self) -> None:
        with self._lock:
            if not self._file.closed:
                self._file.close()


@dataclass
class JournalContents:
    records: list[dict[str, Any]] = field(default_factory=list)
    damaged_lines: list[int] = field(default_factory=list)  # 1-based, unparsable or non-object
    truncated_tail: bool = False  # final line lacked its newline terminator

    @property
    def intact(self) -> bool:
        return not self.damaged_lines and not self.truncated_tail


def iter_lines(path: Path) -> Iterator[tuple[int, bytes, bool]]:
    with open(path, "rb") as handle:
        for number, raw in enumerate(handle, start=1):
            yield number, raw.rstrip(b"\n"), raw.endswith(b"\n")


def read_journal(path: Path) -> JournalContents:
    """Read every intact record; a truncated final record is reported, not fatal."""
    contents = JournalContents()
    path = Path(path)
    if not path.exists():
        return contents
    for number, line, terminated in iter_lines(path):
        if not line:
            continue
        if not terminated:
            contents.truncated_tail = True
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            contents.damaged_lines.append(number)
            continue
        if not isinstance(record, dict):
            contents.damaged_lines.append(number)
            continue
        contents.records.append(record)
    return contents
