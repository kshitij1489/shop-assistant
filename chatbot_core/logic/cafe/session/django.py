from contextlib import contextmanager
from copy import deepcopy
from django.db import transaction
from chatbot_core.scope import required_identity, normalize_platform, session_identity
from chatbot_core.logic.cafe.session.base import BaseSessionStore
from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.intent_handler.base import  BaseIntent
from .browser_lock import browser_turn_lock

class DjangoSessionStore(BaseSessionStore):
    def __init__(self, request, *, tenant_id, platform="website"):
        self.tenant_id = required_identity(tenant_id, "tenant_id")
        self.platform = normalize_platform(platform)
        self._request_session = request.session
        if not request.session.session_key:
            request.session.create()
        self.user_id = required_identity(request.session.session_key, "session_key")
        self._namespace = "cafe:v2:" + session_identity(self.tenant_id, self.platform, self.user_id)
        if self._namespace not in self._request_session:
            self._request_session[self._namespace] = {}

    @property
    def session(self):
        staged = self.staged_snapshot()
        if staged is not None:
            return staged
        return self._request_session.setdefault(self._namespace, {})

    @contextmanager
    def turn_lock(self):
        # Serialize the turn without enclosing checkout or model calls in a
        # database transaction. Checkout commits independently of cache saves.
        from django.contrib.sessions.models import Session
        previous = None
        save = self._request_session.save
        try:
            with browser_turn_lock(self.user_id):
                row = Session.objects.get(session_key=self.user_id)
                previous = row.get_decoded()
                self._request_session._session_cache = deepcopy(previous)
                yield
        except BaseException:
            if previous is not None:
                self._request_session._session_cache = previous
                self._request_session.modified = False
                self._request_session.save = save
            raise

    def read_snapshot(self):
        return self._request_session.get(self._namespace, {})

    def publish_snapshot(self, data):
        from django.contrib.sessions.models import Session
        # Only publication holds a row lock, and merge the current browser
        # data so unrelated namespaces updated outside this turn are retained.
        with transaction.atomic():
            row = Session.objects.select_for_update().get(session_key=self.user_id)
            current = row.get_decoded()
            current[self._namespace] = data
            self._request_session._session_cache = current
            self._request_session.modified = True
            self._request_session.save()
        # Middleware still needs modified for cookie handling, but must not
        # write this snapshot a second time.
        committed = deepcopy(dict(self._request_session))
        save = self._request_session.save

        def save_if_changed(*args, **kwargs):
            if dict(self._request_session) != committed:
                return save(*args, **kwargs)

        self._request_session.save = save_if_changed

    def get_history(self):
        return self.session.get("chat_history", [])

    def set_history(self, history):
        # Cap history to prevent unbounded session growth
        if len(history) > 200:
            history = history[-200:]
        self.session["chat_history"] = history
        self._request_session.modified = True

    def get_counter(self):
        return self.session.get("message_counter", 0)

    def increment_counter(self):
        self.session["message_counter"] = self.get_counter() + 1
        self._request_session.modified = True

    def get_basket(self):
        return Basket.from_dict(self.session.get("basket", {}))

    def set_basket(self, basket):
        self.session["basket"] = basket.to_dict()
        self._request_session.modified = True

    def get_delivery_address(self):
        return self.session.get("delivery_address", {})

    def set_delivery_address(self, delivery_address):
        self.session["delivery_address"] = delivery_address
        self._request_session.modified = True

    def get_checklist(self):
        return self.session.get("checklist", {"payment": False, "order": False, "location": False})

    def set_checklist(self, checklist):
        self.session["checklist"] = checklist
        self._request_session.modified = True

    def get_ongoing_queries(self):
        raw = self.session.get("ongoing_query_queue", [])
        followup_index = self.session.get("awaiting_followup_index")
        return [BaseIntent.from_dict(item) for item in raw], followup_index

    def set_ongoing_queries(self, queue, followup_index):
        self.session["ongoing_query_queue"] = [item.to_dict() for item in queue]
        self.session["awaiting_followup_index"] = followup_index
        self._request_session.modified = True

    def clear_basket(self):
        basket = Basket()
        self.set_basket(basket)
        return basket

    def clear_checklist(self):
        checklist = {"payment": False, "order": False, "location": False, "order_id": None}
        self.set_checklist(checklist)
        return checklist
