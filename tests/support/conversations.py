"""Deterministic Redis transport and intent for conversation tests."""
from types import SimpleNamespace

from chatbot_core.logic.cafe.intent_handler import base as intent_base


class FakeRedis:
    """Only the Redis transport is fake; session keys and serialization are real."""
    def __init__(self):
        self.data = {}
        self.lock_names = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def setex(self, key, ttl, value):
        return self.set(key, value)

    def lock(self, name, **kwargs):
        from threading import Lock
        from uuid import uuid4
        self.lock_names.append(name)
        mutex = self.__dict__.setdefault('mutexes', {}).setdefault(name, Lock())
        token = uuid4().hex

        def acquire(blocking=True):
            acquired = mutex.acquire(blocking=blocking)
            if acquired:
                self.data[name] = token
            return acquired

        def release():
            if self.data.get(name) == token:
                self.data.pop(name)
            mutex.release()

        return SimpleNamespace(acquire=acquire, release=release,
                               extend=lambda *args, **kwargs: self.data.get(name) == token,
                               local=SimpleNamespace(token=token))

    def eval(self, script, numkeys, lock_key, key, token, payload, ttl):
        if self.data.get(lock_key) != token:
            return 0
        self.data[key] = payload
        return 1


class ScriptedIntent(intent_base.BaseIntent):
    """Deterministic business boundary; uses real basket and intent persistence."""
    operations = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.intent_type = "scripted"
        self.promp_restriction = False

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer):
        self.operations.append(self.sub_intent)
        self.is_complete = True
        if self.sub_intent == "add":
            basket.add_item("latte", api_key, "regular", 1)
        elif self.sub_intent == "update":
            if not basket.update_item(1, api_key, quantity=3):
                raise AssertionError("Update ran before add")
        elif self.sub_intent == "ask":
            self.is_complete = False
            self.follow_up_question.append("Which size?")
        elif self.sub_intent == "handoff":
            self.request_handoff("scripted", sub_intent="confirm", follow_up_question=["Confirm order?"])
        elif self.sub_intent == "save":
            delivery_address["city"] = "Delhi"
            checklist["location"] = True
        elif self.sub_intent == "fail":
            raise RuntimeError("Business operation failed")
        return self.sub_intent + " reply", self.query_id

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer):
        self.operations.append("followup:" + query_obj.main_query)
        self.follow_up_reply.append(query_obj.main_query)
        self.is_complete = True
        if query_obj.main_query == "handoff":
            self.request_handoff("scripted", sub_intent="confirm", follow_up_question=["Confirm order?"])
        return "followup reply", self.query_id
