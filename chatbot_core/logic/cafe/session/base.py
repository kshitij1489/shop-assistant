from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json

# Context-local staging also supports reused store instances and nested saves.
_turns = ContextVar("cafe_session_turns", default={})


class BaseSessionStore:
    def staged_snapshot(self):
        return _turns.get().get(id(self))

    @contextmanager
    def turn(self):
        """Read, mutate and publish one isolated snapshot under the session lock.

        Exceptions (including serialization and individual setter failures) leave
        the previous snapshot intact. Nested save_state calls join this turn.
        """
        if self.staged_snapshot() is not None:
            yield self
            return
        with self.turn_lock():
            data = deepcopy(self.read_snapshot())
            token = _turns.set({**_turns.get(), id(self): data})
            try:
                yield self
                # Validate everything before publishing any field.
                snapshot = json.loads(json.dumps(data))
                self.publish_snapshot(snapshot)
            finally:
                _turns.reset(token)

    def get_history(self):
        raise NotImplementedError

    def set_history(self, history):
        raise NotImplementedError

    def get_counter(self):
        raise NotImplementedError

    def increment_counter(self):
        raise NotImplementedError

    def get_basket(self):
        raise NotImplementedError

    def set_basket(self, basket):
        raise NotImplementedError

    def get_delivery_address(self):
        raise NotImplementedError

    def set_delivery_address(self, delivery_address):
        raise NotImplementedError

    def get_checklist(self):
        raise NotImplementedError

    def set_checklist(self, checklist):
        raise NotImplementedError

    def get_ongoing_queries(self):
        raise NotImplementedError

    def set_ongoing_queries(self, queue, followup_index):
        raise NotImplementedError
