"""Tenant/channel adapter for the café conversation graph."""
import logging
from time import monotonic

from .base import BaseTenantHandler
from chatbot_core.logic.cafe.workflow.runner import run_conversation

logger = logging.getLogger(__name__)


class CafeHandler(BaseTenantHandler):
    def __init__(self, tenant, session_store, request=None):
        super().__init__(tenant, request)
        if str(session_store.tenant_id) != str(tenant.id):
            raise ValueError("Session tenant does not match handler tenant")
        self.session = session_store

    def get_basket(self):
        from chatbot_core.logic.cafe.ordering_limits import public_summary
        return public_summary(self.session.get_basket(), self.tenant)

    def handle_message(self, query, customer=None):
        started = monotonic()
        try:
            result = run_conversation(self.tenant, self.session, query, customer)
        except Exception:
            logger.exception(
                "Cafe workflow failed tenant=%s platform=%s duration_ms=%.1f",
                self.tenant.id, self.session.platform, (monotonic() - started) * 1000,
            )
            # A failed turn may already have created an order or initiated payment.
            raise
        logger.info(
            "Cafe workflow completed tenant=%s platform=%s duration_ms=%.1f",
            self.tenant.id, self.session.platform, (monotonic() - started) * 1000,
        )
        return result
