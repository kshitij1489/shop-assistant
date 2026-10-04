import json
from pathlib import Path
import tempfile
import threading
import unittest

from evaluate.contracts.models import (
    ApplicationRevision, EvaluationSummary, ExecutionEvent, ModelNames, RunManifest, StateSnapshot, TurnEvidence,
)
from evaluate.evidence import (
    EvidenceConflict, EvidenceStore, JournalWriter, RedactionError, assert_redacted, build_crash_record,
    find_secrets, read_journal, redact_value,
)

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJ0ZW5hbnRfc2x1ZyI6InFhIn0.abcdefghijklmnopqrstuvwxyz012345"


def event(event_id="evt-1", detail="ok", **overrides) -> ExecutionEvent:
    base = dict(run_id="run-1", scenario_id="qa:q1", scenario_instance_id="inst-1", attempt=1, event_id=event_id,
                occurred_at="2026-09-29T08:30:00+00:00", kind="provision", status="started", detail=detail)
    return ExecutionEvent(**{**base, **overrides})


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.jsonl"

    def test_concurrent_appends_stay_line_intact(self):
        writer = JournalWriter(self.path)
        def work(worker):
            for i in range(200):
                writer.append({"worker": worker, "i": i, "pad": "x" * 500})
        threads = [threading.Thread(target=work, args=(w,)) for w in range(8)]
        for t in threads: t.start()
        for t in threads: t.join()
        writer.flush(); writer.close()
        contents = read_journal(self.path)
        self.assertTrue(contents.intact)
        self.assertEqual(len(contents.records), 1600)
        self.assertEqual(len({(r["worker"], r["i"]) for r in contents.records}), 1600)

    def test_truncated_tail_is_tolerated_and_repaired_on_reopen(self):
        writer = JournalWriter(self.path)
        writer.append({"n": 1}); writer.append({"n": 2}); writer.close()
        with open(self.path, "ab") as handle:
            handle.write(b'{"n": 3, "partial": "tru')
        contents = read_journal(self.path)
        self.assertEqual([r["n"] for r in contents.records], [1, 2])
        self.assertTrue(contents.truncated_tail)
        reopened = JournalWriter(self.path)
        self.assertTrue(reopened.repaired_truncated_tail)
        reopened.append({"n": 4}); reopened.close()
        contents = read_journal(self.path)
        self.assertEqual([r["n"] for r in contents.records], [1, 2, 4])
        self.assertEqual(contents.damaged_lines, [3])
        self.assertFalse(contents.truncated_tail)


class RedactionTests(unittest.TestCase):
    def test_patterns_and_forbidden_keys(self):
        self.assertIn("jwt", find_secrets(f"reply {JWT}"))
        self.assertIn("bearer", find_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz"))
        self.assertIn("session_cookie", find_secrets("Set-Cookie: sessionid=abcdefgh12345678; Path=/"))
        self.assertEqual(find_secrets("Add two Pistachio Ice Cream for 470 rupees"), [])
        with self.assertRaises(RedactionError): assert_redacted({"state": {"headers": {"a": 1}}})
        with self.assertRaises(RedactionError): assert_redacted({"detail": f"token {JWT}"})
        assert_redacted({"detail": "HTTP 401: Missing or invalid Authorization header"})
        masked = redact_value({"message": f"saw {JWT}", "cookie": "x", "nested": ["ok", "sk-" + "a" * 24]})
        self.assertNotIn(JWT, json.dumps(masked))
        self.assertNotIn("cookie", masked)
        self.assertIn("[REDACTED:openai_key]", masked["nested"][1])

    def test_crash_record_redacts_exception_text(self):
        try:
            raise RuntimeError(f"upstream failure with {JWT}")
        except RuntimeError as exc:
            record = build_crash_record(exc, "dispatch", "run-1")
        dumped = record.model_dump_json()
        self.assertNotIn(JWT, dumped)
        self.assertEqual(record.exception_type, "builtins.RuntimeError")
        self.assertTrue(record.traceback)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "run"
        self.store = EvidenceStore(self.directory, "run-1")
        self.addCleanup(self.store.close)

    def test_dedup_identical_and_reject_changed_content(self):
        self.store.write(event()); self.store.write(event())
        with self.assertRaises(EvidenceConflict): self.store.write(event(detail="different"))
        self.store.flush()
        self.assertEqual(len(read_journal(self.directory / "events.jsonl").records), 1)

    def test_rejects_secrets_and_foreign_runs(self):
        with self.assertRaises(RedactionError): self.store.write(event(detail=f"got {JWT}"))
        with self.assertRaises(EvidenceConflict): self.store.write(event(run_id="run-2"))
        self.assertEqual(read_journal(self.directory / "events.jsonl").records, [])

    def test_request_ids_unique_across_turn_evidence(self):
        def turn(event_id, request_id):
            return TurnEvidence(run_id="run-1", scenario_id="qa:q1", scenario_instance_id="inst-1", attempt=1,
                                event_id=event_id, original_turn_index=0, user_turn_index=0, request_id=request_id,
                                message="hi", response_text="hello", http_status=200, elapsed_ms=1.0,
                                snapshot_ids=[], branch="not_applicable")
        self.store.write(turn("evt-a", "req-1"))
        with self.assertRaises(EvidenceConflict): self.store.write(turn("evt-b", "req-1"))
        with self.assertRaises(EvidenceConflict): self.store.write(event(event_id="evt-a"))

    def test_documents_are_write_once(self):
        summary = EvaluationSummary(run_id="run-1", results=[], counts={})
        self.store.write(summary); self.store.write(summary)
        with self.assertRaises(EvidenceConflict):
            self.store.write(EvaluationSummary(run_id="run-1", results=[], counts={"PASS": 0}))

    def test_resume_rebuilds_index_and_reports_truncation(self):
        self.store.write(event()); self.store.flush(); self.store.close()
        with open(self.directory / "events.jsonl", "ab") as handle:
            handle.write(b'{"event_id": "evt-2", "partial')
        resumed = EvidenceStore(self.directory, "run-1")
        self.addCleanup(resumed.close)
        self.assertTrue(resumed.recovered["events.jsonl"].truncated_tail)
        resumed.write(event())  # identical duplicate after resume is a no-op
        with self.assertRaises(EvidenceConflict): resumed.write(event(detail="changed"))

    def test_turn_evidence_requires_known_snapshots(self):
        turn = TurnEvidence(run_id="run-1", scenario_id="qa:q1", scenario_instance_id="inst-1", attempt=1,
                            event_id="evt-turn", original_turn_index=0, user_turn_index=0, request_id="req-1",
                            message="hi", response_text="hello", http_status=200, elapsed_ms=1.0,
                            snapshot_ids=["snap-missing"], branch="not_applicable")
        with self.assertRaises(EvidenceConflict):
            self.store.write(turn)
        snapshot = StateSnapshot(run_id="run-1", scenario_id="qa:q1", scenario_instance_id="inst-1", attempt=1,
                                 event_id="evt-snap", snapshot_id="snap-1", original_turn_index=0, request_id=None,
                                 phase="before", captured_at="2026-09-29T08:30:00+00:00", state={}, unavailable_sections=[])
        self.store.write(snapshot)
        self.store.write(turn.model_copy(update={"snapshot_ids": ["snap-1"]}))

    def test_rebuilt_manifest_keeps_the_original_timestamp(self):
        manifest = RunManifest(
            run_id="run-1", application=ApplicationRevision(commit="a" * 40, working_tree_fingerprint="b" * 64, dirty=False),
            dataset_hashes={}, configuration_hash="c" * 64, scenario_plan_hash="d" * 64, scenario_plan_version="baseline-v1",
            models=ModelNames(chat="chat", translate="translate", analytics="analytics"), scenario_ids=[],
            created_at="2026-09-29T08:30:00+00:00")
        self.store.write(manifest)
        self.store.write(manifest.model_copy(update={"created_at": "2026-09-29T09:00:00+00:00"}))
        stored = json.loads((self.directory / "manifest.json").read_text())
        self.assertEqual(stored["created_at"], "2026-09-29T08:30:00+00:00")
        with self.assertRaises(EvidenceConflict):
            self.store.write(manifest.model_copy(update={"scenario_plan_version": "other-v1"}))


if __name__ == "__main__":
    unittest.main()
