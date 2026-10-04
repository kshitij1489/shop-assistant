from .base import BaseTenantHandler

class RetailHandler(BaseTenantHandler):
    def handle_message(self, message):
        # Logic for retail / e-commerce tenants
        return f"[RetailBot] Processing message: {message}"
