from chatbot_core.logic.cafe.basket import Basket
from chatbot_core.logic.cafe.query import Query

class SessionMixin:
    def _get_session_history(self):
        if not self.request:
            return []
        return self.request.session.get("chat_history", [])

    def _get_message_counter(self):
        if not self.request:
            return 0
        return self.request.session.get("message_counter", 0)

    def _increment_session_counter(self):
        if not self.request:
            return
        session = self.request.session
        session["message_counter"] = session.get("message_counter", 0) + 1
        session.modified = True

    def _update_session_history(self, data):
        if not self.request:
            return
        history = self._get_session_history()
        history.append(data)
        self.request.session["chat_history"] = history
        self.request.session.modified = True

    def _load_basket(self):
        if not self.request:
            self.basket = Basket()
        else:
            data = self.request.session.get("basket", {})
            self.basket = Basket.from_dict(data)

    def _save_basket(self, basket):
        if self.request:
            self.request.session["basket"] = basket.to_dict()
            self.request.session.modified = True

    def _load_ongoing_query_queue(self):
        if not self.request:
            self.ongoing_query_queue = []
            self.awaiting_followup_index = None
            return

        queue_data = self.request.session.get("ongoing_query_queue", [])
        self.ongoing_query_queue = [BaseIntent.from_dict(item) for item in queue_data if item]
        self.awaiting_followup_index = self.request.session.get("awaiting_followup_index")

    def _save_ongoing_query_queue(self):
        if not self.request:
            return
        data = [item.to_dict() for item in self.ongoing_query_queue]
        self.request.session["ongoing_query_queue"] = data
        self.request.session["awaiting_followup_index"] = self.awaiting_followup_index
        self.request.session.modified = True
