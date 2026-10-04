from threading import RLock
from django.utils.timezone import now
from chatbot_core.scope import required_identity, normalize_platform, session_identity
from chatbot_core.logic.cafe.session.base import BaseSessionStore
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.base import BaseIntent

_session_data = {}  # {user_id: {session_data}}
_session_locks = {}
_MAX_CHAT_HISTORY = 200  # cap per-user chat history to prevent unbounded memory growth

class MemorySessionStore(BaseSessionStore):
    def __init__(self, user_id, *, tenant_id, platform):
        self.user_id = required_identity(user_id, "user_id")
        self.tenant_id = required_identity(tenant_id, "tenant_id")
        self.platform = normalize_platform(platform)
        self._storage_id = session_identity(self.tenant_id, self.platform, self.user_id)
        _session_data.setdefault(self._storage_id, {
            "chat_history": [],
            "message_counter": 0,
            "basket": {},
            "delivery_address": {},
            "checklist": {"payment": False, "order": False, "location": False, "order_id": None},
            "ongoing_query_queue": [],
            "awaiting_followup_index": None,
            "last_activity_at": None,
        })

    def turn_lock(self):
        return _session_locks.setdefault(self._storage_id, RLock())

    def read_snapshot(self):
        return _session_data[self._storage_id]

    def publish_snapshot(self, data):
        _session_data[self._storage_id] = data

    def _store(self):
        staged = self.staged_snapshot()
        return staged if staged is not None else self.read_snapshot()

    def get_history(self):
        return self._store()["chat_history"]

    def set_history(self, history):
        # Keep only the most recent messages to prevent unbounded memory growth
        if len(history) > _MAX_CHAT_HISTORY:
            history = history[-_MAX_CHAT_HISTORY:]
        self._store()["chat_history"] = history

    def get_counter(self):
        return self._store()["message_counter"]

    def increment_counter(self):
        self._store()["message_counter"] += 1
        self._store()["last_activity_at"] = now().isoformat()

    def get_basket(self):
        return Basket.from_dict(self._store()["basket"])

    def set_basket(self, basket):
        self._store()["basket"] = basket.to_dict()

    def clear_basket(self):
        self._store()["basket"] = {}
        return self.get_basket()

    def get_delivery_address(self):
        return self._store()["delivery_address"]

    def set_delivery_address(self, delivery_address):
        self._store()["delivery_address"] = delivery_address

    def get_checklist(self):
        return self._store()["checklist"]

    def set_checklist(self, checklist):
        self._store()["checklist"] = checklist

    def clear_checklist(self):
        self._store()["checklist"] = {"payment": False, "order": False, "location": False, "order_id": None}
        return self.get_checklist()

    def get_ongoing_queries(self):
        raw_queue = self._store().get("ongoing_query_queue", [])

        queue = []
        for i, q in enumerate(raw_queue):
            try:
                obj = BaseIntent.from_dict(q)
                queue.append(obj)
            except Exception as e:
                import traceback
                print(f"!!! Failed at query #{i} with error: {e}")
                traceback.print_exc()
                # re-raise so you still see the 500, but with context
                raise

        return queue, self._store().get("awaiting_followup_index")


    def set_ongoing_queries(self, queue, followup_index):
        self._store()["ongoing_query_queue"] = [q.to_dict() for q in queue]
        self._store()["awaiting_followup_index"] = followup_index
