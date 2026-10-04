"""Read every application journal written for this run. Missing evidence stays unknown."""
from __future__ import annotations

from pathlib import Path

from evaluate.contracts.models import ExecutionIdentity
from evaluate.controls.usage import ApplicationUsage
from evaluate.runner.ports import Usage


class JournalUsage:
    """`UsageSource` that re-reads `application-*.jsonl` after workers have flushed."""

    def __init__(self, provisioner, directory: Path) -> None:
        self.provisioner = provisioner
        self.directory = Path(directory)

    def usage(self, lease, request_id: str) -> Usage | None:
        paths = sorted(self.directory.glob("application-*.jsonl"))
        if not paths:
            return None
        source = ApplicationUsage(self.provisioner, self._identity, paths)
        return source.usage(lease, request_id)

    def _identity(self, lease) -> ExecutionIdentity:
        _tenant, owner = self.provisioner.owned(lease)
        return ExecutionIdentity(
            run_id=owner["run_id"], scenario_id=owner["scenario_id"],
            scenario_instance_id=owner["instance_id"], attempt=owner["attempt"],
        )
