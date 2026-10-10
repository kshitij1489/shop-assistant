"""Exercise real FAISS with synthetic vectors; no model downloads or providers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch
from types import SimpleNamespace

import numpy as np
from django.core.cache import cache
from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import Sum
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from chatbot_core.models import FaissVector, SemanticCacheEntry, SemanticCacheState
from chatbot_core.vector_store import semantic_cache as service
from chatbot_core.vector_store.config import policy
from chatbot_core.vector_store.faiss_index import ScopedIndexes, indexes, unit_vector


@override_settings(SEMANTIC_CACHE_ENABLED=True, EMBEDDING_DIMENSION=3)
class SemanticCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        indexes.clear()
        self.addCleanup(indexes.clear)
        self.encoder = self.enterContext(patch.object(service, "get_embedding", return_value=[1., 0., 0.]))

    def lookup(self, question="Where are you located?", scope="tenant-a", knowledge="Facts", model="model-a", ttl=60):
        return service.lookup(model, scope, knowledge, question, ttl)

    def save(self, question="Where are you located?", answer="Delhi", **kwargs):
        _, _, ctx = self.lookup(question, **kwargs)
        with self.captureOnCommitCallbacks(execute=True):
            service.store(ctx, answer, kwargs.get("ttl", 60), kwargs.get("model", "model-a"))
        return ctx

    def test_scope_filtering_precedes_nearest_neighbours(self):
        valid = self.save()
        for i in range(6):
            self.save(scope=f"other-tenant-{i}", answer="Foreign")
        cache.clear()
        self.assertEqual(self.lookup("Please where are you located?")[:2], (True, "Delhi"))
        for changes in ({"scope": "tenant-b"}, {"model": "other"}, {"knowledge": "New facts"}):
            self.assertFalse(self.lookup(**changes)[0])
        self.assertEqual(SemanticCacheEntry.objects.get(partition=valid["partition"]).hit_count, 1)

    def test_exact_hit_needs_no_encoder_and_survives_redis_loss(self):
        self.save()
        cache.clear()
        with patch.object(service, "get_embedding", side_effect=AssertionError("Should not encode")):
            self.assertEqual(self.lookup()[:2], (True, "Delhi"))

    def test_no_extension_of_absolute_expiry_on_semantic_or_exact_hit(self):
        now = timezone.now()
        with patch.object(service.timezone, "now", return_value=now):
            self.save(ttl=10)
        with patch.object(service.timezone, "now", return_value=now + timedelta(seconds=8)):
            self.assertTrue(self.lookup("Please where are you located?", ttl=600)[0])
        with patch.object(service.timezone, "now", return_value=now + timedelta(seconds=11)):
            self.assertFalse(self.lookup("Please where are you located?", ttl=600)[0])
            self.assertFalse(self.lookup()[0])

    def test_commit_is_required_for_hot_cache_and_rollback_removes_vector(self):
        ctx = self.lookup()[2]
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            service.store(ctx, "Delhi", 60, "model-a")
        self.assertIsNone(cache.get(ctx["sig"]))
        self.assertEqual(len(callbacks), 2)
        self.assertEqual(FaissVector.objects.count(), 1)
        other = self.lookup("Another question?")[2]
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                service.store(other, "Other", 60, "model-a")
                raise RuntimeError("Rollback")
        self.assertFalse(SemanticCacheEntry.objects.filter(sig=other["sig"]).exists())
        self.assertIsNone(cache.get(other["sig"]))

    def test_partial_vector_failure_is_atomic_and_retry_repairs_missing_vector(self):
        ctx = self.lookup()[2]
        with patch.object(FaissVector.objects, "update_or_create", side_effect=ValueError("fail")):
            with self.assertLogs(service.logger, "WARNING"):
                self.assertEqual(service.store(ctx, "Delhi", 60, "model-a"), "Delhi")
        self.assertEqual(SemanticCacheEntry.objects.count(), 0)
        self.assertIsNone(cache.get(ctx["sig"]))
        service.store(ctx, "Delhi", 60, "model-a")
        FaissVector.objects.all().delete()
        service.store(ctx, "Delhi", 60, "model-a")
        self.assertEqual(SemanticCacheEntry.objects.count(), 1)
        self.assertEqual(FaissVector.objects.count(), 1)

    @override_settings(SEMANTIC_CACHE_MAX_ROWS=3, SEMANTIC_CACHE_MAX_PARTITION_ROWS=2)
    def test_global_and_partition_caps_evict_lru_and_cascade_vectors(self):
        self.save("Old?")
        self.save("Recent?")
        self.save("Newest?")
        self.assertFalse(SemanticCacheEntry.objects.filter(normalized_query="old?").exists())
        self.save(scope="other")
        self.save(scope="third")
        self.assertEqual(SemanticCacheEntry.objects.count(), 3)
        self.assertEqual(FaissVector.objects.count(), 3)

    @override_settings(SEMANTIC_CACHE_MAX_DB_BYTES=2200)
    def test_byte_budget_and_oversized_response_admission(self):
        self.save("First?", answer="a" * 100)
        self.save("Second?", answer="b" * 100)
        self.assertEqual(SemanticCacheEntry.objects.count(), 1)
        self.assertLessEqual(SemanticCacheEntry.objects.aggregate(size=Sum("size_bytes"))["size"], 2200)
        self.save("Huge?", answer="x" * 3000)
        self.assertEqual(SemanticCacheEntry.objects.count(), 1)

    def test_worker_snapshots_observe_committed_revision(self):
        ctx = self.save()
        worker = ScopedIndexes()
        args = dict(partition=ctx["partition"], embedding_id=ctx["embedding_id"], dimension=3)
        before, _ = worker.search([1, 0, 0], **args)
        self.save("Please where are you located?", answer="Second")
        after, _ = worker.search([1, 0, 0], **args)
        self.assertEqual(len(before), 1)
        self.assertEqual(len(after), 2)
        self.assertEqual(worker.stats()["builds"], 2)

    def test_revision_change_during_build_retries_without_losing_new_vector(self):
        ctx = self.save()
        worker = ScopedIndexes()
        build = worker._build
        called = False

        def racing_build(*args):
            nonlocal called
            snapshot = build(*args)
            if not called:
                called = True
                self.save("Please where are you located?", answer="Added during build")
            return snapshot

        with patch.object(worker, "_build", side_effect=racing_build):
            ids, scores = worker.search([1, 0, 0], partition=ctx["partition"],
                                       embedding_id=ctx["embedding_id"], dimension=3)
        self.assertEqual(len(ids), 2)
        self.assertEqual(scores, [1., 1.])
        self.assertEqual(worker.stats()["builds"], 2)

    def test_model_revision_change_never_reuses_same_dimension_vectors(self):
        self.save()
        with override_settings(EMBEDDING_REVISION="a" * 40):
            self.assertFalse(self.lookup()[0])
        with override_settings(EMBEDDING_DIMENSION=2):
            with patch.object(service, "get_embedding", return_value=[1., 0.]):
                self.assertFalse(self.lookup()[0])

    def test_malformed_persisted_vectors_are_skipped(self):
        self.save()
        FaissVector.objects.update(vector=b"invalid")
        cache.clear()
        with self.assertLogs("chatbot_core.vector_store.faiss_index", "WARNING"):
            self.assertFalse(self.lookup("Please where are you located?")[0])
        self.assertGreater(indexes.stats()["invalid_vectors"], 0)

    def test_invalid_query_vectors_fail_open_without_admission(self):
        for vector in ([0, 0, 0], [np.nan, 0, 0], [np.inf, 0, 0], [1, 0]):
            with self.subTest(vector=vector):
                with self.assertRaises(ValueError):
                    unit_vector(vector, 3)
                with patch.object(service, "get_embedding", return_value=vector):
                    with self.assertLogs(service.logger, "WARNING"):
                        hit, _, ctx = self.lookup()
                self.assertFalse(hit)
                self.assertTrue(ctx["skip_durable"])

    def test_changed_negation_quantity_entity_order_or_language_never_reuses_answer(self):
        for first, second in (
            ("Is it dairy free?", "Is it not dairy free?"),
            ("Price of 2 tubs", "Price of 3 tubs"),
            ("Is it below <5?", "Is it below >5?"),
            ("Does it cost 1.5?", "Does it cost 1 5?"),
            ("Is it open Mon-Fri?", "Is it open Mon Fri?"),
            ("Does milk contain chocolate?", "Does chocolate contain milk?"),
            ("Where is the cafe?", "कैफे कहाँ है?"),
        ):
            self.assertFalse(service.equivalent_queries(first, second))
        self.assertTrue(service.equivalent_queries("Please where are you located?", "where are you located."))
        self.save("Is it dairy free?", answer="Yes")
        cache.clear()
        self.assertFalse(self.lookup("Is it not dairy free?")[0])

    def test_prune_removes_expired_and_legacy_and_versions_other_workers(self):
        self.save(ttl=1)
        SemanticCacheEntry.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        SemanticCacheEntry.objects.create(sig="legacy", scope="legacy", kb_fp="legacy", response="legacy")
        before = SemanticCacheState.objects.get(pk=1).revision
        self.assertEqual(service.prune(), 2)
        self.assertEqual(FaissVector.objects.count(), 0)
        self.assertGreater(SemanticCacheState.objects.get(pk=1).revision, before)

    def test_prune_applies_lowered_scope_cap_without_new_writes(self):
        self.save("First?")
        self.save("Second?")
        with override_settings(SEMANTIC_CACHE_MAX_PARTITION_ROWS=1):
            self.assertEqual(service.prune(), 1)
        self.assertEqual(SemanticCacheEntry.objects.count(), 1)

    @override_settings(SEMANTIC_CACHE_MAX_ROWS=2)
    def test_redis_hits_keep_hot_entry_recent_for_durable_lru(self):
        ctx = self.save("Hot?")
        self.save("Cold?")
        self.assertTrue(self.lookup("Hot?")[0])
        self.save("New?")
        self.assertTrue(SemanticCacheEntry.objects.filter(sig=ctx["sig"]).exists())
        self.assertFalse(SemanticCacheEntry.objects.filter(normalized_query="cold?").exists())

    def test_disabled_cache_and_nonpositive_ttl_do_not_embed_or_write_db(self):
        with override_settings(SEMANTIC_CACHE_ENABLED=False):
            ctx = self.save()
            self.assertTrue(self.lookup()[0])
            self.assertIsNotNone(cache.get(ctx["sig"]))
        self.assertFalse(self.lookup(ttl=0)[0])
        self.assertEqual(self.lookup(ttl=0)[2], {})
        self.encoder.assert_not_called()
        self.assertEqual(SemanticCacheEntry.objects.count(), 0)

    def test_redis_failure_preserves_durable_lookup_and_generated_response(self):
        with patch.object(service.cache, "get", side_effect=ConnectionError), \
                patch.object(service.cache, "set", side_effect=ConnectionError):
            with self.assertLogs(service.logger, "WARNING"):
                self.save()
                self.assertEqual(self.lookup()[:2], (True, "Delhi"))

    def test_evaluation_cold_mode_bypasses_every_tier_and_writes(self):
        with patch("evaluate.controls.cache.current", return_value=SimpleNamespace(cache_mode="cold")), \
                patch("evaluate.controls.cache.emit"):
            self.assertEqual(self.lookup(), (False, None, {}))
            self.assertEqual(service.store({}, "Answer", 60, "model-a"), "Answer")
        self.encoder.assert_not_called()
        self.assertEqual(SemanticCacheEntry.objects.count(), 0)

    def test_equally_similar_foreign_id_is_revalidated_against_partition(self):
        self.save(scope="foreign", answer="Private")
        foreign_id = SemanticCacheEntry.objects.get().pk
        cache.clear()
        with patch.object(service, "search", return_value=([foreign_id], [1.])):
            self.assertFalse(self.lookup()[0])


@override_settings(SEMANTIC_CACHE_ENABLED=True, EMBEDDING_DIMENSION=3,
                   SEMANTIC_CACHE_MAX_ROWS=2)
class SemanticCacheConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_admissions_enforce_uniqueness_and_global_capacity(self):
        cache.clear()
        indexes.clear()
        barrier = Barrier(4)

        def write(number, same):
            close_old_connections()
            try:
                scope = "same" if same else f"tenant-{number}"
                _, _, ctx = service.lookup("model-a", scope, "Facts", "Question?", 60)
                barrier.wait(timeout=20)
                service.store(ctx, "Answer", 60, "model-a")
            finally:
                close_old_connections()

        with patch.object(service, "get_embedding", return_value=[1., 0., 0.]):
            for same in (True, False):
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(write, number, same) for number in range(4)]
                    for future in futures:
                        future.result(timeout=30)
                self.assertEqual(SemanticCacheEntry.objects.count(), 1 if same else 2)
                self.assertEqual(FaissVector.objects.count(), 1 if same else 2)
                self.assertEqual(SemanticCacheState.objects.get(pk=1).revision, 4 if same else 8)
        indexes.clear()


class SemanticCacheMigrationTests(TransactionTestCase):
    def test_upgrade_excludes_duplicate_unversioned_legacy_rows(self):
        if settings.MIGRATION_MODULES.get("chatbot_core", "default") is None:
            self.skipTest("Requires the real migration profile")
        previous = [("chatbot_core", "0024_tenantinfo_city_tenantinfo_city_place_id_and_more")]
        current = [("chatbot_core", "0025_bounded_semantic_cache")]
        executor = MigrationExecutor(connection)
        executor.migrate(previous)
        try:
            old_apps = executor.loader.project_state(previous).apps
            Entry = old_apps.get_model("chatbot_core", "SemanticCacheEntry")
            Vector = old_apps.get_model("chatbot_core", "FaissVector")
            for _ in range(2):
                entry = Entry.objects.create(sig="duplicate", scope="legacy", kb_fp="old",
                                             normalized_query="Where?", response="Old answer")
                Vector.objects.create(cache_entry=entry, dim=384, vector=b"unknown model")
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(current)
        self.assertEqual(SemanticCacheEntry.objects.filter(partition="").count(), 2)
        self.assertEqual(service.prune(), 2)
        self.assertEqual(FaissVector.objects.count(), 0)


@override_settings(SEMANTIC_CACHE_ENABLED=True, EMBEDDING_DIMENSION=3)
class SemanticCacheTransactionTests(TransactionTestCase):
    def setUp(self):
        cache.clear()
        indexes.clear()
        self.addCleanup(indexes.clear)
        self.enterContext(patch.object(service, "get_embedding", return_value=[1., 0., 0.]))

    def lookup(self, question="Where are you located?", scope="tenant-a"):
        return service.lookup("model-a", scope, "Facts", question, 60)

    def save(self, question="Where are you located?", **kwargs):
        ctx = self.lookup(question, **kwargs)[2]
        service.store(ctx, "Delhi", 60, "model-a")
        return ctx

    def test_lookup_never_publishes_uncommitted_answers(self):
        for question in ("Where are you located?", "Please where are you located?"):
            with self.subTest(question=question):
                with self.assertRaisesMessage(RuntimeError, "Rollback"):
                    with transaction.atomic():
                        self.save()
                        hit, answer, ctx = self.lookup(question)
                        self.assertEqual((hit, answer), (True, "Delhi"))
                        self.assertIsNone(cache.get(ctx["sig"]))
                        raise RuntimeError("Rollback")
                self.assertEqual(SemanticCacheEntry.objects.count(), 0)
                self.assertIsNone(cache.get(ctx["sig"]))
                self.assertFalse(self.lookup(question)[0])

    def test_exact_only_fallback_also_waits_for_commit(self):
        with override_settings(SEMANTIC_CACHE_ENABLED=False):
            with self.assertRaisesMessage(RuntimeError, "Rollback"):
                with transaction.atomic():
                    ctx = self.save()
                    self.assertIsNone(cache.get(ctx["sig"]))
                    raise RuntimeError("Rollback")
            self.assertIsNone(cache.get(ctx["sig"]))

    def test_lookup_promotion_uses_original_deadline_after_commit(self):
        self.save()
        entry = SemanticCacheEntry.objects.get()
        question = "Please where are you located?"
        with transaction.atomic():
            hit, _, ctx = self.lookup(question)
            self.assertTrue(hit)
            self.assertIsNone(cache.get(ctx["sig"]))
        self.assertEqual(cache.get(ctx["sig"])["expires"], entry.expires_at.timestamp())
        cache.clear()
        with patch.object(service.timezone, "now", return_value=entry.expires_at - timedelta(seconds=1)) as now:
            with transaction.atomic():
                self.assertTrue(self.lookup(question)[0])
                now.return_value = entry.expires_at + timedelta(seconds=1)
        self.assertIsNone(cache.get(ctx["sig"]))

    def test_hot_answer_expiring_during_lru_update_is_not_returned(self):
        self.save()
        deadline = SemanticCacheEntry.objects.get().expires_at
        with patch.object(service.timezone, "now", return_value=deadline - timedelta(seconds=1)) as now:
            def slow_touch(*args):
                now.return_value = deadline + timedelta(seconds=1)

            with patch.object(service, "_touch", side_effect=slow_touch):
                self.assertFalse(self.lookup()[0])

    def test_rolled_back_index_cannot_match_a_later_committed_revision(self):
        ctx = self.save()
        worker = ScopedIndexes()
        args = dict(partition=ctx["partition"], embedding_id=ctx["embedding_id"], dimension=3)
        worker.search([1, 0, 0], **args)
        with self.assertRaisesMessage(RuntimeError, "Rollback"):
            with transaction.atomic():
                self.save("Rolled back question?")
                self.assertEqual(len(worker.search([1, 0, 0], **args)[0]), 2)
                raise RuntimeError("Rollback")
        with patch.object(service, "get_embedding", return_value=[0., 1., 0.]):
            self.save("Committed question?")
        ids, _ = worker.search([1, 0, 0], **args)
        self.assertEqual(set(ids), set(SemanticCacheEntry.objects.values_list("pk", flat=True)))
        ids, scores = worker.search([0, 1, 0], k=1, **args)
        self.assertEqual(ids, [SemanticCacheEntry.objects.get(normalized_query="committed question?").pk])
        self.assertEqual(scores, [1.])

    def test_lru_failure_does_not_break_the_callers_transaction(self):
        self.save()

        def failed_update(**kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM missing_semantic_cache_table")

        with transaction.atomic():
            with patch("django.db.models.query.QuerySet.update", side_effect=failed_update):
                with self.assertLogs(service.logger, "WARNING"):
                    self.assertEqual(self.lookup()[:2], (True, "Delhi"))
            self.assertEqual(SemanticCacheEntry.objects.count(), 1)

    def test_lookup_database_errors_do_not_break_the_callers_transaction(self):
        self.save()
        cache.clear()

        def failed_read(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM missing_semantic_cache_table")

        # Exercise both the durable exact read and a FAISS snapshot DB read.
        for operation in ("first", "iterator"):
            with self.subTest(operation=operation), transaction.atomic():
                with patch(f"django.db.models.query.QuerySet.{operation}", side_effect=failed_read):
                    with self.assertLogs(service.logger, "WARNING"):
                        hit, _, ctx = self.lookup("Please where are you located?")
                self.assertFalse(hit)
                self.assertTrue(ctx["skip_durable"])
                self.assertEqual(SemanticCacheEntry.objects.count(), 1)

    def test_autocommit_searches_reuse_the_snapshot(self):
        ctx = self.save()
        worker = ScopedIndexes()
        args = dict(partition=ctx["partition"], embedding_id=ctx["embedding_id"], dimension=3)
        before = worker.search([1, 0, 0], **args)
        self.assertEqual(worker.search([1, 0, 0], **args), before)
        self.assertEqual(worker.stats()["builds"], 1)
        self.assertEqual(worker.stats()["indexes"], 1)

    @override_settings(SEMANTIC_CACHE_MAX_INDEXES=1)
    def test_index_lru_and_byte_limit(self):
        self.save()
        self.save(scope="other")
        cache.clear()
        self.lookup("Please where are you located?")
        self.lookup("Please where are you located?", scope="other")
        self.assertEqual(indexes.stats()["indexes"], 1)
        indexes.clear()
        cache.clear()
        with override_settings(SEMANTIC_CACHE_MAX_INDEX_BYTES=100):
            self.assertFalse(self.lookup("Please where are you located?")[0])
            self.assertEqual(indexes.stats()["estimated_bytes"], 0)
