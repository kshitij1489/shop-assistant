import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pydantic import ValidationError

from evaluate.__main__ import main
from evaluate.contracts.models import (
    EvaluationSummary, RunConfiguration, SCHEMAS, ScenarioPlan, Setup,
)
from evaluate.datasets.loader import DatasetError, load_dataset, read_json, resolve_profiles
from evaluate.identity import application_revision, canonical_hash, instance_id
from evaluate.manifest import behavioral_configuration, build_manifest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / "test_data"


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        shutil.copytree(DATA, self.data)
        self.raw = read_json(self.data / "session_query_sets.json")
        self.plan_raw = read_json(ROOT / "datasets/baseline.plan.json")

    def load(self):
        (self.data / "session_query_sets.json").write_text(json.dumps(self.raw))
        return load_dataset(self.data, ScenarioPlan.model_validate(self.plan_raw))

    def reviewed_action(self):
        # Exercise a real reviewed action without claiming an executor exists.
        source = next(s for s in self.raw["sessions"] if s["id"] == "s129_checkout_price_changed")
        sid = "sessions:" + source["id"]
        action = {
            "action_id": "change-price", "scenario_id": sid,
            "original_turn_index": source["before_turn"][0]["turn_index"],
            "requirement_ref": "/before_turn/0/action",
            "requirement_hash": canonical_hash(source["before_turn"][0]["action"]),
            "review_ref": "test-fixture-review-v1",
            "operation": {"kind": "catalog_control", "item_name": "Pistachio Ice Cream", "variant_name": "QA standard", "price_minor": 47000},
        }
        self.plan_raw["scenarios"][sid] = {
            "source_hash": canonical_hash(source), "review_ref": "test-fixture-review-v1",
            "actions": [action], "requirements": {"/before_turn/0/action": ["change-price"]},
        }
        return sid, action

    def test_load_all_counts_and_never_replay_references(self):
        bundle = self.load()
        self.assertEqual(bundle.counts["scenarios"], 219)
        self.assertEqual(bundle.counts["user_turns"], 723)
        self.assertEqual(bundle.counts["knowledge_records"], 33)
        self.assertEqual(len(bundle.blocked), 219)
        scenario = bundle.scenarios[8]
        self.assertEqual([t.original_turn_index for t in scenario.turns], [0, 2])
        self.assertEqual([t.user_turn_index for t in scenario.turns], [0, 1])
        self.assertEqual(scenario.turns[1].answers_ask_from_turn, 1)
        self.assertEqual(len(scenario.references), 2)
        self.assertTrue(all(s.namespace in {"qa", "sessions"} for s in bundle.scenarios))

    def test_counts_ignore_declared_counts(self):
        self.raw["user_turns"] = 1
        bundle = self.load()
        self.assertEqual(bundle.counts["session_user_turns"], 704)
        self.assertIn("stale_count", [w.code for w in bundle.warnings])

    def test_knowledge_profiles_can_be_ready_after_capability_resolution(self):
        self.plan_raw["supported_capabilities"] = ["knowledge_import", "freeze_clock"]
        bundle = self.load()
        qa = [s for s in bundle.scenarios if s.namespace == "qa"]
        self.assertTrue(qa)
        self.assertTrue(all(not s.blockers for s in qa))

    def test_malformed_and_overlapping_knowledge(self):
        name = self.raw["source_knowledge"][0]
        path = self.data / name
        original = path.read_text()
        path.write_text('{"intent": 12}')
        with self.assertRaises(DatasetError): self.load()
        path.write_text(original)
        self.raw["source_knowledge"].append("knowledge_base.json")
        with self.assertRaises(DatasetError): self.load()

    def test_duplicate_and_malformed_ids(self):
        for bad in (self.raw["sessions"][1]["id"], "space ID", ""):
            with self.subTest(bad=bad):
                self.raw["sessions"][0]["id"] = bad
                with self.assertRaises(DatasetError): self.load()

    def test_namespaces_allow_same_source_id(self):
        qa = read_json(self.data / "qa_test_cases.json")
        qa[0]["id"] = self.raw["sessions"][0]["id"]
        (self.data / "qa_test_cases.json").write_text(json.dumps(qa))
        ids = [s.scenario_id for s in self.load().scenarios]
        self.assertEqual(len(ids), len(set(ids)))

    def test_reference_only_session_is_rejected(self):
        self.raw["sessions"][0]["turns"] = [self.raw["sessions"][0]["turns"][1]]
        with self.assertRaises(DatasetError): self.load()

    def test_invalid_reference_roles(self):
        for change in ({"reference_only": False}, {"speaker": "system"}):
            self.raw["sessions"][0]["turns"][1].update(change)
            with self.assertRaises(DatasetError): self.load()

    def test_user_reference_only_is_rejected(self):
        self.raw["sessions"][0]["turns"][0]["reference_only"] = True
        with self.assertRaises(DatasetError): self.load()

    def test_bad_turn_and_action_references(self):
        self.raw["sessions"][8]["turns"][2]["answers_ask_from_turn"] = 0
        with self.assertRaises(DatasetError): self.load()
        self.raw["sessions"][8]["turns"][2]["answers_ask_from_turn"] = 1
        self.raw["sessions"][0]["before_turn"] = [{"turn_index": 1, "action": "arbitrary text"}]
        with self.assertRaises(DatasetError): self.load()

    def test_unknown_profile_and_contradictory_profiles(self):
        for profiles in (["bogus"], ["knowledge_only", "catalog_sandbox"]):
            self.raw["sessions"][0]["setup_profiles"] = profiles
            with self.assertRaises(DatasetError): self.load()

    def test_invalid_clock(self):
        for clock in ("tomorrow", "2026-09-29T14:00:00", "2026-09-29T14:00:00+00:00"):
            self.raw["sessions"][0]["clock"] = clock
            with self.assertRaises(DatasetError): self.load()

    def test_coverage_checks(self):
        self.raw["knowledge_coverage"][next(iter(self.raw["knowledge_coverage"]))] = ["missing"]
        with self.assertRaises(DatasetError): self.load()

    def test_missing_knowledge_pointer(self):
        self.raw["sessions"][0]["knowledge_refs"] = ["knowledge_base.json#/missing"]
        with self.assertRaises(DatasetError): self.load()

    def test_path_escape(self):
        self.raw["source_knowledge"] = ["../outside.json"]
        with self.assertRaises(DatasetError): self.load()

    def test_missing_action_blocks_instead_of_guessing(self):
        bundle = self.load()
        scenario = next(s for s in bundle.scenarios if s.source_id == "s129_checkout_price_changed")
        self.assertFalse(scenario.actions)
        self.assertIn("unmapped_requirement", [b.code for b in scenario.blockers])

    def test_reviewed_action_and_unknown_capability(self):
        sid, _ = self.reviewed_action()
        scenario = next(s for s in self.load().scenarios if s.scenario_id == sid)
        self.assertEqual(scenario.actions[0].operation.price_minor, 47000)
        self.assertIn("unsupported_action", [b.code for b in scenario.blockers])

    def test_missing_action_definition_is_error(self):
        sid, _ = self.reviewed_action()
        self.plan_raw["scenarios"][sid]["actions"] = []
        with self.assertRaises(DatasetError): self.load()

    def test_wrong_action_target_is_error(self):
        _, action = self.reviewed_action()
        action["original_turn_index"] = 11
        with self.assertRaises(DatasetError): self.load()

    def test_stale_review_is_error(self):
        _, action = self.reviewed_action()
        action["requirement_hash"] = "0" * 64
        with self.assertRaises(DatasetError): self.load()

    def test_unknown_override_rejected(self):
        sid, _ = self.reviewed_action()
        self.plan_raw["scenarios"][sid]["overrides"] = {"execute_shell": "no"}
        with self.assertRaises(ValidationError): self.load()

    def test_contradictory_payment_override_rejected(self):
        sid, _ = self.reviewed_action()
        self.plan_raw["scenarios"][sid]["overrides"] = {"payment": "unavailable", "payment_methods": ["online"]}
        with self.assertRaises(DatasetError): self.load()

    def test_profile_precedence_and_explicit_false_zero(self):
        plan = ScenarioPlan.model_validate(self.plan_raw)
        for names in (["catalog_sandbox", "checkout_sandbox"], ["checkout_sandbox", "catalog_sandbox"]):
            setup, _ = resolve_profiles(names, plan, Setup(delivery_fee_minor=0, scheduling=False))
            self.assertEqual(setup.payment, "fake_adapter")
            self.assertEqual(setup.payment_methods, ["cash", "online"])
            self.assertEqual(setup.delivery_fee_minor, 0)
            self.assertIs(setup.scheduling, False)

    def test_unrelated_profile_conflict_and_cycle(self):
        self.plan_raw["profiles"]["address_sandbox"]["defaults"]["payment"] = "fake_adapter"
        plan = ScenarioPlan.model_validate(self.plan_raw)
        with self.assertRaises(DatasetError): resolve_profiles(["catalog_sandbox", "address_sandbox"], plan)
        self.plan_raw["profiles"]["catalog_sandbox"]["extends"] = ["checkout_sandbox"]
        with self.assertRaises(DatasetError): self.load()

    def test_unsupported_mock_setup_detected(self):
        self.plan_raw["supported_capabilities"] = ["requires_finite_stock", "mock_standard_test_variant"]
        scenario = self.load().scenarios[0]
        self.assertTrue({"stock_mismatch", "variant_mismatch"} <= {b.code for b in scenario.blockers})

    def test_allergy_followup_points_at_the_documented_question(self):
        scenario = next(s for s in self.load().scenarios if s.source_id == "s159_hi_allergy_phir_payment")
        self.assertNotIn("undefined_question", [b.code for b in scenario.blockers])
        self.assertEqual(scenario.turns[2].answers_ask_from_turn, 1)
        self.assertEqual(scenario.references[0].asks, "Do you want a different item?")

    def test_stable_identity_survives_reordering(self):
        before = self.load()
        self.raw["sessions"].reverse()
        after = self.load()
        self.assertEqual({s.scenario_id: s.source_hash for s in before.scenarios}, {s.scenario_id: s.source_hash for s in after.scenarios})
        sid = before.scenarios[0].scenario_id
        self.assertEqual(instance_id("run-a", sid), instance_id("run-a", sid))
        self.assertNotEqual(instance_id("run-a", sid), instance_id("run-b", sid))
        self.assertNotEqual(instance_id("run-a", sid, 0), instance_id("run-a", sid, 1))

    def test_strict_json_duplicate_keys_and_nonfinite(self):
        path = self.data / "broken.json"
        for content in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '{'):
            path.write_text(content)
            with self.assertRaises(DatasetError): read_json(path)

    def test_cli_exit_codes(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["validate", "--dataset", str(self.data)]), 0)
            self.assertEqual(main(["validate", "--dataset", str(self.data), "--require-ready"]), 0)
            (self.data / "qa_test_cases.json").write_text("{}")
            self.assertEqual(main(["validate", "--dataset", str(self.data)]), 1)


class ContractTests(unittest.TestCase):
    def test_samples_and_schema_exports(self):
        for name, model in SCHEMAS.items():
            with self.subTest(name=name):
                model.model_validate(read_json(ROOT / "examples" / f"{name}.json"))
                schema = read_json(ROOT / "contracts/schemas" / f"{name}.schema.json")
                schema.pop("$schema")
                schema.pop("$id")
                self.assertEqual(schema, model.model_json_schema())

    def test_no_secrets_in_configuration(self):
        config = read_json(ROOT / "examples/run_configuration.json")
        for key in ("api_key", "authorization", "env", "jwt", "password"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                RunConfiguration.model_validate({**config, key: "test-sensitive-value"})
        for url in ("https://user:pass@example.test", "https://example.test?api_key=secret"):
            with self.assertRaises(ValidationError): RunConfiguration.model_validate({**config, "base_url": url})

    def test_summary_counts_must_match(self):
        with self.assertRaises(ValidationError):
            EvaluationSummary(run_id="x", results=[], counts={"PASS": 1})

    def test_unknown_schema_version_rejected(self):
        config = read_json(ROOT / "examples/run_configuration.json")
        with self.assertRaises(ValidationError):
            RunConfiguration.model_validate({**config, "schema_version": "2.0.0"})

    def test_working_tree_fingerprint_and_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def git(*args):
                return subprocess.run(["git", "-C", temp, *args], check=True, capture_output=True)
            git("init")
            (root / "app.py").write_text("initial\n")
            (root / ".gitignore").write_text(".env\n")
            git("add", ".")
            git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "initial")
            original = application_revision(root)
            self.assertFalse(original.dirty)
            (root / "app.py").write_text("changed\n")
            changed = application_revision(root)
            self.assertNotEqual(original.working_tree_fingerprint, changed.working_tree_fingerprint)
            self.assertTrue(changed.dirty)
            (root / ".env").write_text("SECRET=never-serialized")
            self.assertEqual(changed.working_tree_fingerprint, application_revision(root).working_tree_fingerprint)
            (root / "untracked.py").write_text("new\n")
            self.assertNotEqual(changed.working_tree_fingerprint, application_revision(root).working_tree_fingerprint)
            config = RunConfiguration.model_validate(read_json(ROOT / "examples/run_configuration.json"))
            plan = ScenarioPlan.model_validate(read_json(ROOT / "datasets/baseline.plan.json"))
            manifest = build_manifest(root, config, plan, load_dataset(DATA, plan))
            self.assertEqual(manifest.configuration_hash, canonical_hash(behavioral_configuration(config)))
            other = config.model_copy(update={"run_id": "another-run-id"})
            self.assertEqual(manifest.configuration_hash,
                             canonical_hash(behavioral_configuration(other)))
            self.assertNotIn("never-serialized", manifest.model_dump_json())
            self.assertEqual(len(manifest.scenario_ids), 219)


if __name__ == "__main__":
    unittest.main()
