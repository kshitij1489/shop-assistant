from .base import BaseTenantHandler


class BookingsHandler(BaseTenantHandler):
    def handle_message(self, message):
        return f"[BookingsBot] Processing message: {message}"
