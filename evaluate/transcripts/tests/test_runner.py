"""Wrapper contracts, using the evaluation framework's existing HTTP test server."""
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from evaluate.transcripts.__main__ import _prepare_environment, _run_live, _verify_stack, main
from evaluate.transcripts.parser import DEFAULT_DATASET, parse_test_cases, suite_names
from evaluate.transcripts.runner import run_sessions
from evaluate.transcripts.transcript import EMULATOR_HOLD, place_held_sessions, requires_location_emulator
from evaluate.runner import Components, ProjectionSettlementPolicy, SnapshotBranchOracle
from evaluate.runner.transport import WebsiteChatResponse, WebsiteTransport
from evaluate.evidence.journal import read_journal
from evaluate.tests.chat_server import ChatServer
from evaluate.tests.fakes import FakeControls, FakeInspector, FakeProvisioner, make_action, make_config, make_scenario


class ParserTests(unittest.TestCase):
    def test_all_user_queries_preserve_source_order_and_qa_is_isolated(self):
        sessions, sanity = parse_test_cases()
        source = json.loads((DEFAULT_DATASET / "session_query_sets.json").read_text())
        qa = json.loads((DEFAULT_DATASET / "qa_test_cases.json").read_text())
        self.assertEqual([case.source_id for case in sessions], [case["id"] for case in source["sessions"]])
        for case, original in zip(sessions, source["sessions"]):
            self.assertEqual([turn.text for turn in case.turns],
                             [turn["text"] for turn in original["turns"] if turn["speaker"] == "user"])
        self.assertEqual([[turn.text for turn in case.turns] for case in sanity], [[case["question"]] for case in qa])

    def test_selection_aliases_quotes_duplicates_and_invalid_names(self):
        self.assertEqual(suite_names(["session sanity", "sessions"]), ["sessions", "sanity"])
        self.assertEqual(suite_names(["sanity"]), ["sanity"])
        for values in (["typo"], [""]):
            with self.assertRaises(ValueError):
                suite_names(values)

    def test_dry_run_does_not_initialize_live_components(self):
        output = io.StringIO()
        with patch("django.setup") as setup, redirect_stdout(output):
            self.assertEqual(main(["sanity", "--dry-run"]), 0)
        setup.assert_not_called()
        qa = json.loads((DEFAULT_DATASET / "qa_test_cases.json").read_text())
        self.assertEqual(json.loads(output.getvalue())["queries"], len(qa))

    def test_dry_run_text_addresses_do_not_need_location_emulator(self):
        output = io.StringIO()
        with patch("django.setup"), redirect_stdout(output):
            self.assertEqual(main(["sessions", "--dry-run"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["emulator_only"], [])

    def test_targeted_rerun_selects_only_requested_sessions_in_source_order(self):
        output = io.StringIO()
        with redirect_stdout(output), patch("django.setup") as setup:
            code = main(["sessions", "--scenario", "sessions:s136_new_order_after_terminal",
                         "--scenario", "s92_empty_noise_recovery", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["sessions"], 2)
        self.assertEqual(json.loads(output.getvalue())["queries"], 7)
        setup.assert_not_called()
        with patch("evaluate.transcripts.__main__._run_live", return_value=0) as live:
            main(["sessions", "--scenario", "s136_new_order_after_terminal", "--scenario", "s92_empty_noise_recovery"])
        self.assertEqual([case.source_id for case in live.call_args.args[1]],
                         ["s92_empty_noise_recovery", "s136_new_order_after_terminal"])

    def test_unknown_or_out_of_suite_scenario_fails_before_live_execution(self):
        with patch("evaluate.transcripts.__main__._run_live") as live, redirect_stderr(io.StringIO()):
            self.assertEqual(main(["sessions", "--scenario", "typo"]), 2)
            self.assertEqual(main(["sanity", "--scenario", "s92_empty_noise_recovery"]), 2)
        live.assert_not_called()

    def test_bundled_address_scenarios_have_no_geocoding_faults(self):
        sessions, sanity = parse_test_cases()
        held = [case.source_id for case in [*sessions, *sanity] if requires_location_emulator(case)]
        self.assertEqual(held, [])

    def test_held_sessions_stay_in_source_order_without_execution_errors(self):
        first = make_scenario("s01", ["hello"])
        held = make_scenario("s36_addresses_default_and_map_pin", ["pin"])
        last = make_scenario("s02", ["again"])
        document = {"run_id": "run", "sessions": [
            {"session_id": "session_1", "source_id": "s01", "suite": "sessions",
             "queries": [{"query": "hello", "llm_answer": "echo", "executed": True}]},
            {"session_id": "session_2", "source_id": "s02", "suite": "sessions",
             "queries": [{"query": "again", "llm_answer": "echo", "executed": True}]},
        ]}
        placed = place_held_sessions(document, [first, held, last], {held.scenario_id})
        self.assertEqual([row["source_id"] for row in placed["sessions"]],
                         ["s01", "s36_addresses_default_and_map_pin", "s02"])
        self.assertEqual([row["session_id"] for row in placed["sessions"]],
                         ["session_1", "session_2", "session_3"])
        self.assertEqual(placed["sessions"][1]["skipped"], EMULATOR_HOLD)
        self.assertIsNone(placed["sessions"][1]["queries"][0]["llm_answer"])
        self.assertFalse(placed["sessions"][1]["queries"][0]["executed"])
        self.assertNotIn("errors", placed["sessions"][1])


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.config = make_config(SimpleNamespace(url="http://127.0.0.1:8000"))

    def test_environment_preparation_preserves_provider_or_settings_default(self):
        for provider in (None, "emulator", "openstreetmap"):
            with self.subTest(provider=provider), patch.dict(os.environ, {}, clear=True), patch("django.setup"):
                if provider is not None:
                    os.environ["EVALUATION_LOCATION_PROVIDER"] = provider
                _prepare_environment(self.config, Path(tempfile.gettempdir()))
                self.assertEqual(os.environ.get("EVALUATION_LOCATION_PROVIDER"), provider)

    def test_both_providers_require_successful_stack_preflight(self):
        for provider in ("emulator", "openstreetmap"):
            for ready in (True, False):
                with self.subTest(provider=provider, ready=ready), \
                        patch("evaluate.transcripts.__main__.evaluation_location_provider", return_value=provider), \
                        patch("evaluate.transcripts.__main__.models_match", return_value=True), \
                        patch("evaluate.integration.preflight.run_preflight", return_value={"valid": ready}) as preflight, \
                        redirect_stderr(io.StringIO()):
                    directory = Path(tempfile.gettempdir()) / "transcript-preflight"
                    self.assertEqual(_verify_stack(self.config, directory), ready)
                    preflight.assert_called_once_with(base_url=self.config.base_url, evidence_directory=directory)

    def test_unsupported_provider_is_rejected_before_preflight(self):
        with patch("evaluate.transcripts.__main__.evaluation_location_provider", return_value="unsupported"), \
                patch("evaluate.integration.preflight.run_preflight") as preflight:
            with self.assertRaisesRegex(ValueError, "emulator or openstreetmap"):
                _verify_stack(self.config, Path(tempfile.gettempdir()))
        preflight.assert_not_called()


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.server = ChatServer().start()
        self.addCleanup(self.server.stop)
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.output = Path(directory) / "transcripts.json"
        self.config = make_config(self.server)

        class Provisioner(FakeProvisioner):
            def cleanup(self, lease, *, force=False):
                super().cleanup(lease)

        self.provisioner = Provisioner(self.server)
        self.transport = WebsiteTransport(self.server.url, self.provisioner, 0.5)
        self.advance = Mock()
        self.transport.advance = self.advance
        self.components = Components(provisioner=self.provisioner, transport=self.transport,
                                     controls=FakeControls(), evaluator=Mock(),
                                     branch_oracle=SnapshotBranchOracle(), inspector=FakeInspector(self.server),
                                     settlement=ProjectionSettlementPolicy())
        self.enterContext(redirect_stderr(io.StringIO()))

    def run_cases(self, *cases):
        return run_sessions(self.config, list(cases), self.components, self.output)["sessions"]

    def run_cli_cases(self, provider, cases):
        directory = self.output.parent / "cli-run"
        config = self.config.model_copy(update={"scenario_plan_version": cases[0].scenario_plan_version})
        output = io.StringIO()
        # Keep the real runner/HTTP transport; replace Django provisioning and process logging.
        modules = {
            "evaluate.integration.compose": SimpleNamespace(build_components=Mock(return_value=self.components)),
            "evaluate.integration.telemetry": SimpleNamespace(install_process_journal=Mock()),
        }
        with patch.dict("sys.modules", modules), \
                patch("evaluate.transcripts.__main__._prepare_environment", return_value=directory), \
                patch("evaluate.transcripts.__main__._verify_stack", return_value=True), \
                patch("evaluate.transcripts.__main__.evaluation_location_provider", return_value=provider), \
                redirect_stdout(output):
            code = _run_live(config, cases, directory.parent)
        self.assertEqual(code, 0)
        return json.loads((directory / "transcripts.json").read_text()), json.loads(output.getvalue())

    def test_text_address_sessions_execute_with_coverage_actions(self):
        sessions, _ = parse_test_cases()
        cases = [case for case in sessions if case.source_id.startswith(("s36_", "s117_", "s118_"))]
        document, report = self.run_cli_cases("emulator", cases)
        self.assertEqual([row["source_id"] for row in document["sessions"]], [case.source_id for case in cases])
        self.assertEqual(report["sessions"], 3)
        self.assertEqual(report["queries"], 11)
        self.assertEqual(report["executed_queries"], 11)
        self.assertEqual(report["skipped_sessions"], 0)
        self.assertEqual(report["execution_errors"], 0)
        self.assertEqual([action_id for _, action_id in self.components.controls.applied],
                         [action.action_id for case in cases for action in case.actions])
        self.assertTrue(all(query["llm_answer"] and query["executed"]
                            for row in document["sessions"] for query in row["queries"]))
        self.assertTrue(all("skipped" not in row for row in document["sessions"]))

    def test_text_address_sessions_are_independent_of_geocoding_provider(self):
        sessions, _ = parse_test_cases()
        cases = [case for case in sessions if case.source_id.startswith(("s36_", "s117_", "s118_"))]
        document, report = self.run_cli_cases("openstreetmap", cases)
        self.assertEqual(report["sessions"], 3)
        self.assertEqual(report["skipped_sessions"], 0)
        self.assertEqual(report["executed_queries"], 11)
        self.assertEqual(report["execution_errors"], 0)
        self.assertEqual(len(self.provisioner.provisioned), 3)
        self.assertTrue(all("skipped" not in row for row in document["sessions"]))

    def test_sequential_http_conversations_keep_cookies_and_skip_reference_answers(self):
        case = make_scenario("first", ["hello", "yes"], references={1: "A reference question?"}, answers={1: 1})
        case.turns[1].turn_kind = "followup"
        rows = self.run_cases(case, make_scenario("second", ["new conversation"]))
        self.assertEqual([row["llm_answer"] for row in rows[0]["queries"]], ["echo: hello", "echo: yes"])
        self.assertEqual([row["is_followup"] for row in rows[0]["queries"]], [False, True])
        self.assertEqual(sorted(len(session.messages) for session in self.server.state.sessions.values()), [1, 2])
        self.assertEqual(len(self.provisioner.cleaned), 2)
        self.assertFalse(self.provisioner.leases)
        self.components.evaluator.evaluate.assert_not_called()
        self.assertEqual(json.loads(self.output.read_text())["sessions"], rows)

    def test_setup_and_before_turn_actions_use_original_indexes(self):
        actions = [make_action("setup", {"kind": "payment_control", "operation": "fail"}),
                   make_action("before", {"kind": "payment_control", "operation": "fail"}, original_turn_index=2)]
        case = make_scenario("actions", ["hello", "again"], references={1: "reference"}, actions=actions)
        observed = []
        self.advance.side_effect = lambda lease: observed.append([action for _, action in self.components.controls.applied])
        self.run_cases(case)
        self.assertEqual(observed, [["setup"], ["setup", "before"]])

    def test_failed_request_is_not_replayed_and_later_sessions_still_run(self):
        rows = self.run_cases(make_scenario("broken", ["[disconnect]", "unsent"]),
                              make_scenario("next", ["hello"]))
        self.assertEqual(rows[0]["queries"][0]["error"], "connection")
        self.assertFalse(rows[0]["queries"][1]["executed"])
        self.assertEqual(rows[1]["queries"][0]["llm_answer"], "echo: hello")
        self.assertEqual(sum(len(session.messages) for session in self.server.state.sessions.values()), 2)

    def test_reply_is_saved_before_provider_failure_and_cleanup(self):
        def advance(lease):
            saved = read_journal(self.output.parent / "turns.jsonl").records
            self.assertEqual(saved[0]["response_text"], "echo: hello")
            raise RuntimeError("mock payment stopped")
        self.advance.side_effect = advance
        rows = self.run_cases(make_scenario("provider", ["hello", "unsent"]))
        self.assertEqual(rows[0]["queries"][0]["llm_answer"], "echo: hello")
        self.assertTrue(rows[0]["errors"])
        self.assertEqual(len(self.provisioner.cleaned), 1)

    def test_setup_failure_retains_every_query_and_continues(self):
        self.provisioner.provision = Mock(side_effect=[RuntimeError("unavailable"), RuntimeError("unavailable")])
        rows = self.run_cases(make_scenario("one", ["hello", "again"]), make_scenario("two", ["new"]))
        self.assertEqual([len(row["queries"]) for row in rows], [2, 1])
        self.assertTrue(all(row["errors"] for row in rows))

    def test_interrupt_preserves_completed_replies_and_releases_resources(self):
        self.advance.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.run_cases(make_scenario("interrupted", ["hello", "unsent"]))
        saved = json.loads(self.output.read_text())["sessions"][0]
        self.assertEqual(saved["queries"][0]["llm_answer"], "echo: hello")
        self.assertFalse(saved["queries"][1]["executed"])
        self.assertFalse(self.provisioner.leases)

    def test_ambiguous_address_session_is_ready(self):
        from evaluate.scenarios.plan import readiness
        sessions, _ = parse_test_cases()
        case = next(case for case in sessions if case.source_id == "s115_address_ambiguous_selection")
        self.assertEqual(readiness(case), [])
        self.assertEqual(case.turns[0].text, "Use Home or Work.")

    def test_readiness_blocks_duplicate_address_fixture_before_provision(self):
        from evaluate.fixtures.definitions import FIXTURES, Fixture
        FIXTURES["dup-labels"] = Fixture(kind="addresses", addresses=["home64", "home57"])
        self.addCleanup(FIXTURES.pop, "dup-labels", None)
        action = make_action("seed", {"kind": "seed_fixture", "fixture_id": "dup-labels", "fixture_hash": "b" * 64})
        case = make_scenario("dup-labels", ["Use Home or Work."], actions=[action])
        with patch.object(self.provisioner, "provision", wraps=self.provisioner.provision) as provision:
            rows = self.run_cases(case)
        provision.assert_not_called()
        self.assertIn("duplicate_address_label", rows[0]["errors"][0]["message"])
        self.assertTrue(all(row["llm_answer"] is None for row in rows[0]["queries"]))

    def test_transport_prepares_and_retries_only_not_dispatched(self):
        original_send = self.transport.send
        responses = [WebsiteChatResponse(None, None, 0, "connection", dispatch_state="not_dispatched")]

        def send(lease, request):
            return responses.pop() if responses else original_send(lease, request)

        with patch.object(self.transport, "prepare", wraps=self.transport.prepare) as prepare, \
                patch.object(self.transport, "send", side_effect=send) as dispatch:
            rows = self.run_cases(make_scenario("retry", ["hello"]))
        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(dispatch.call_count, 2)
        self.assertEqual(rows[0]["queries"][0]["llm_answer"], "echo: hello")
        self.assertNotIn("error", rows[0]["queries"][0])
        self.assertEqual(len(read_journal(self.output.parent / "turns.jsonl").records), 2)

    def test_s132_reaches_restore_while_payment_is_pending(self):
        sessions, _ = parse_test_cases()
        case = next(case for case in sessions if case.source_id == "s132_online_provider_unavailable")
        self.components.inspector.payment_sequence = ["pending"]
        # No pump_until_idle hook exists: the wrapper must use transport.advance.
        rows = self.run_cases(case)
        self.assertEqual([action_id for _, action_id in self.components.controls.applied],
                         [action.action_id for action in case.actions])
        self.assertEqual(self.advance.call_count, len(case.turns))
        self.assertTrue(all(row["executed"] for row in rows[0]["queries"]))
        self.assertNotIn("errors", rows[0])

    def test_success_has_no_verdict_and_no_assertion_results(self):
        self.run_cases(make_scenario("unscored", ["hello"]))
        attempts = read_journal(self.output.parent / "attempts.jsonl").records
        self.assertEqual(attempts[0]["execution_status"], "COMPLETED")
        self.assertIsNone(attempts[0]["evaluation_verdict"])
        self.assertEqual(read_journal(self.output.parent / "assertions.jsonl").records, [])

    def test_empty_input_rejections_are_recorded_without_inventing_llm_answers(self):
        sessions, _ = parse_test_cases()
        case = next(case for case in sessions if case.source_id == "s92_empty_noise_recovery")
        document, report = self.run_cli_cases("emulator", [case])
        queries = document["sessions"][0]["queries"]
        self.assertEqual(report["executed_queries"], 4)
        self.assertEqual(report["expected_rejections"], 2)
        self.assertEqual(report["execution_errors"], 0)
        for row in queries[:2]:
            self.assertEqual(row["http_status"], 400)
            self.assertEqual(row["response_error"], "Missing message")
            self.assertEqual(row["expected_rejection"], "empty_input")
            self.assertIsNone(row["llm_answer"])
            self.assertNotIn("error", row)
        self.assertTrue(all(row["llm_answer"] for row in queries[2:]))

    def test_other_http_errors_and_unreviewed_empty_inputs_remain_errors(self):
        rows = self.run_cases(make_scenario("unreviewed", ["", "[status-500]"]))
        self.assertTrue(all("error" in row and "expected_rejection" not in row for row in rows[0]["queries"]))
        from evaluate.transcripts.transcript import _record_reply
        for message in (None, "Invalid token payload", "Missing message"):
            row = {}
            evidence = {"http_status": 500 if message == "Missing message" else 400,
                        "response_text": None, "response_error": message}
            _record_reply(row, evidence, {}, allow_empty_rejection=True)
            self.assertIn("error", row)
            self.assertNotIn("expected_rejection", row)
