from .base import BaseTenantHandler


class InformationalHandler(BaseTenantHandler):
    def handle_message(self, message):
        return f"[InformationalBot] Processing message: {message}"
