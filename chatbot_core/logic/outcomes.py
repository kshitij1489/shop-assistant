"""Persisted task outcomes, independent of routing and response wording."""
from enum import Enum


class TaskOutcome(str, Enum):
    NEEDS_CLARIFICATION = 'needs_clarification'
    COMPLETED = 'completed'
    TERMINAL_REJECTION = 'terminal_rejection'
    TEMPORARILY_BLOCKED = 'temporarily_blocked'

    @property
    def resumable(self):
        return self in {self.NEEDS_CLARIFICATION, self.TEMPORARILY_BLOCKED}
