"""Stitch checks that do not start Django or call a model."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from evaluate.contracts.interfaces import Blocked, ChatRequest, Lease
from evaluate.contracts.models import ExecutionIdentity
from evaluate.integration.command import execute, stitch_report
from evaluate.integration.routing import control_lane
from evaluate.runner.ports import WebsiteCredential
from evaluate.runner.transport import WebsiteTransport
from evaluate.tests.chat_server import ChatServer, SessionState
from evaluate.tests.fakes import make_turn

ROOT = Path(__file__).resolve().parents[2]


class RoutingTests(unittest.TestCase):
    def test_application_lane_is_clock_and_classifier_coverage_only(self):
        self.assertEqual(control_lane("freeze_clock"), "application")
        self.assertEqual(control_lane("lookup_control", "classification"), "application")
        self.assertEqual(control_lane("lookup_control", "coverage"), "application")
        self.assertEqual(control_lane("lookup_control", "geocoding"), "fixtures")
        self.assertEqual(control_lane("lookup_control", "reverse_geocoding"), "fixtures")
        self.assertEqual(control_lane("payment_control"), "fixtures")
        self.assertEqual(control_lane("catalog_control"), "fixtures")
        self.assertEqual(control_lane("seed_fixture"), "fixtures")


class TransportBindingTests(unittest.TestCase):
    def setUp(self):
        self.server = ChatServer().start()
        self.addCleanup(self.server.stop)
        self.server.state.register_tenant("qa-tenant", "qa-tenant-private-api-key-0123456789")
        self.server.state.sessions["evalsession01"] = SessionState("evalsession01", "customer-owned")
        self.lease = Lease("lease-1", "instance-1")
        self.identity = ExecutionIdentity(
            run_id="run-1", scenario_id="sessions:s1", scenario_instance_id="instance-1", attempt=1)

    def transport(self, header):
        class Resolver:
            def resolve(self, lease):
                return WebsiteCredential("qa-tenant", "qa-tenant-private-api-key-0123456789")

        return WebsiteTransport(
            self.server.url, Resolver(), 1.0,
            session_cookie=("sessionid", lambda lease: "evalsession01"),
            evaluation_header=("X-Evaluation-Context", header),
        )

    def send(self, transport, text="hello"):
        return transport.send(self.lease, ChatRequest(self.identity, "req-1", make_turn(0, 0, text)))

    def test_owned_cookie_and_context_header_are_sent_once(self):
        seen = []

        def header(lease, request):
            seen.append(request.request_id)
            return "signed-ticket-value"

        response = self.send(self.transport(header))
        self.assertEqual(response.response_text, "echo: hello")
        self.assertEqual(seen, ["req-1"])
        self.assertEqual(self.server.state.evaluation_headers, ["signed-ticket-value"])
        self.assertIn("sessionid=evalsession01", self.server.state.cookie_headers[0])
        self.assertEqual(self.server.state.sessions["evalsession01"].messages, ["hello"])
        self.assertNotIn("signed-ticket-value", response.response_text or "")

    def test_rejected_context_header_does_not_reach_the_server(self):
        transport = self.transport(lambda lease, request: "bad\nvalue")
        with self.assertRaises(Blocked):
            self.send(transport)
        self.assertEqual(self.server.state.token_requests, 0)
        self.assertEqual(self.server.state.evaluation_headers, [])


class CommandGuardTests(unittest.TestCase):
    def test_default_run_lists_blocked_scenarios_without_live_chat(self):
        report = stitch_report(ROOT / "test_data")
        self.assertFalse(report["live_chat"])
        self.assertFalse(report["paid_model_calls"])
        self.assertEqual(report["blocked_scenarios"], [])
        rendered = json.dumps(report)
        self.assertNotIn("api_key", rendered)

    def test_live_chat_refuses_a_remote_origin_and_an_in_repo_directory(self):
        remote = self._config("http://example.com")
        self.assertEqual(execute(self._args(remote, Path("/tmp/studio-eval-remote"))), 2)
        local = self._config("http://127.0.0.1:8000")
        self.assertEqual(execute(self._args(local, ROOT / "evaluate" / "integration")), 2)

    def _config(self, base_url: str) -> Path:
        host = base_url.split("//", 1)[-1].replace(":", "-").replace("/", "")
        path = Path("/tmp") / f"studio-eval-{host}.json"
        path.write_text(json.dumps({
            "schema_version": "1.0.0", "run_id": "guard-run", "base_url": base_url,
            "dataset_directory": "test_data", "scenario_plan_version": "dataset-setup-v2-text-address",
            "models": {"chat": "gpt-6-luna", "translate": "gpt-6-luna", "analytics": "gpt-6-luna"},
        }))
        self.addCleanup(path.unlink)
        return path

    def _args(self, config: Path, output: Path):
        return type("Args", (), {
            "allow_live_chat": True, "dataset": ROOT / "test_data", "config": config,
            "output": output, "scenario": [], "cache": "cold",
        })()
