"""The café adapter invokes the graph once and propagates failures without replay."""
import importlib
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from chatbot_core.logic.tenant_handlers import cafe_handler


class CafeHandlerTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.enterClassContext(patch("chatbot_core.vector_store.embedding_client.get_embedding",
                                    side_effect=AssertionError("Unexpected embedding request")))
        cls.runner = importlib.import_module("chatbot_core.logic.cafe.workflow.runner")

    def handler(self, tenant_id=1):
        return cafe_handler.CafeHandler(
            SimpleNamespace(id=tenant_id),
            SimpleNamespace(tenant_id=str(tenant_id), platform="telegram"),
        )

    def test_graph_runs_once_per_turn_for_each_tenant(self):
        for tenant_id in (1, 2, "20"):
            with self.subTest(tenant_id=tenant_id):
                handler = self.handler(tenant_id)
                customer = SimpleNamespace(tenant_id=tenant_id)
                with patch.object(cafe_handler, "run_conversation", return_value=("reply", [])) as run:
                    self.assertEqual(handler.handle_message("hello", customer), ("reply", []))
                run.assert_called_once_with(handler.tenant, handler.session, "hello", customer)

    def test_failure_is_logged_and_propagated_without_retry(self):
        handler = self.handler()
        failure = ConnectionError("service unavailable")
        with patch.object(cafe_handler, "run_conversation", side_effect=failure) as run, \
             self.assertLogs(cafe_handler.logger, level="ERROR") as logs:
            with self.assertRaises(ConnectionError) as caught:
                handler.handle_message("confirm order")
        self.assertIs(caught.exception, failure)
        run.assert_called_once_with(handler.tenant, handler.session, "confirm order", None)
        self.assertIn("tenant=1 platform=telegram duration_ms=", logs.output[0])

    def test_success_log_identifies_workflow_without_message_content(self):
        handler = self.handler()
        with patch.object(cafe_handler, "run_conversation", return_value=("reply", [])), \
             self.assertLogs(cafe_handler.logger, level="INFO") as logs:
            handler.handle_message("private customer message")
        self.assertIn("tenant=1 platform=telegram duration_ms=", logs.output[0])
        self.assertNotIn("private customer message", logs.output[0])
