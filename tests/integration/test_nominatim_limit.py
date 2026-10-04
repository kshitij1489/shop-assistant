"""Real Redis/Lua regressions; opt in with a disposable NOMINATIM_TEST_REDIS_URL."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest import skipUnless
from unittest.mock import Mock, patch
from uuid import uuid4

import redis
from django.test import SimpleTestCase, override_settings

from chatbot_core.logic.cafe import nominatim, nominatim_limit

PUBLIC = "https://nominatim.openstreetmap.org"
REDIS_URL = os.environ.get("NOMINATIM_TEST_REDIS_URL")


@skipUnless(REDIS_URL, "Set NOMINATIM_TEST_REDIS_URL to a disposable Redis instance")
@override_settings(NOMINATIM_BASE_URL=PUBLIC, NOMINATIM_API_KEY="", NOMINATIM_TIMEOUT_SECONDS=2,
                   NOMINATIM_USER_AGENT="StudioDeskTest/1.0", NOMINATIM_MAX_QUEUE_SECONDS=3,
                   NOMINATIM_MIN_INTERVAL_SECONDS="")
class RedisDispatchTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache.backends.locmem import LocMemCache
        self.enterContext(patch.object(nominatim, "cache", LocMemCache(uuid4().hex, {})))
        self.client = redis.Redis.from_url(REDIS_URL, decode_responses=True,
                                          socket_connect_timeout=1, socket_timeout=1)
        self.addCleanup(self.client.close)
        self.client.ping()
        self.enterContext(patch.object(nominatim_limit, "_client", self.client))
        self.enterContext(patch.object(nominatim_limit, "_KEY_PREFIX", "test:nominatim:" + uuid4().hex))
        self.key = nominatim_limit._key(PUBLIC)
        self.addCleanup(self.client.delete, self.key + ":owner", self.key + ":cooldown")

    def test_delayed_owner_cannot_burst_with_another_worker(self):
        acquired = threading.Event()
        waiting = threading.Event()
        resume = threading.Event()
        timestamps = []
        original = nominatim_limit._command

        def command(script, base, *args):
            result = original(script, base, *args)
            if script == nominatim_limit._ACQUIRE:
                if result == 0 and not acquired.is_set():
                    acquired.set()
                    if not resume.wait(5):
                        raise AssertionError("Owner was not resumed")
                elif result == -1:
                    waiting.set()
            return result

        def http(*args, **kwargs):
            timestamps.append(time.monotonic())
            response = Mock(status_code=200)
            response.json.return_value = []
            return response

        with patch.object(nominatim_limit, "_command", side_effect=command), \
                patch.object(nominatim.requests, "get", side_effect=http), \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(nominatim.search_postal_code, "122102", "in")
            try:
                self.assertTrue(acquired.wait(2))
                second = pool.submit(nominatim.lookup_osm_object, "N123")
                self.assertTrue(waiting.wait(2))
                # Simulate the first Redis reply/worker being delayed after acquisition.
                time.sleep(0.2)
                self.assertEqual(timestamps, [])
            finally:
                resume.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual(len(timestamps), 2)
        self.assertGreaterEqual(timestamps[1] - timestamps[0], 1.0)

    def test_waiting_worker_obeys_429_from_current_owner(self):
        http_entered = threading.Event()
        waiting = threading.Event()
        resume = threading.Event()
        original = nominatim_limit._command

        def command(script, base, *args):
            result = original(script, base, *args)
            if script == nominatim_limit._ACQUIRE and result == -1:
                waiting.set()
            return result

        def http(*args, **kwargs):
            http_entered.set()
            if not resume.wait(5):
                raise AssertionError("HTTP response was not resumed")
            return Mock(status_code=429, headers={"Retry-After": "120"})

        with patch.object(nominatim_limit, "_command", side_effect=command), \
                patch.object(nominatim.requests, "get", side_effect=http) as request, \
                ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(nominatim.search_postal_code, "122102", "in")
            try:
                self.assertTrue(http_entered.wait(2))
                second = pool.submit(nominatim.search_postal_code, "122011", "in")
                self.assertTrue(waiting.wait(2))
            finally:
                resume.set()
            self.assertIsNone(first.result(timeout=5))
            self.assertIsNone(second.result(timeout=5))
            request.assert_called_once()
        self.assertGreater(self.client.pttl(self.key + ":cooldown"), 118_000)
        self.assertFalse(self.client.exists(self.key + ":owner"))

    def test_long_cooldown_survives_release_and_is_not_shortened(self):
        self.assertEqual(nominatim_limit._command(nominatim_limit._ACQUIRE, PUBLIC, "owner"), 0)
        self.client.psetex(self.key + ":cooldown", 120_000, "cooldown")
        self.assertEqual(nominatim_limit._command(nominatim_limit._FINISH, PUBLIC, "owner", 1000), 0)
        self.assertGreater(self.client.pttl(self.key + ":cooldown"), 119_000)
        self.assertGreater(nominatim_limit._command(nominatim_limit._ACQUIRE, PUBLIC, "next"), 119_000)

    def test_wrong_owner_cannot_release_or_shorten_cooldown(self):
        self.assertEqual(nominatim_limit._command(nominatim_limit._ACQUIRE, PUBLIC, "owner"), 0)
        self.assertEqual(nominatim_limit._command(nominatim_limit._FINISH, PUBLIC, "wrong", 1000), -1)
        self.assertEqual(self.client.get(self.key + ":owner"), "owner")
        self.assertEqual(nominatim_limit._command(nominatim_limit._ACQUIRE, PUBLIC, "next"), -1)

    def test_abandoned_owner_does_not_expire_and_allows_no_http(self):
        self.assertEqual(nominatim_limit._command(nominatim_limit._ACQUIRE, PUBLIC, "abandoned"), 0)
        self.assertEqual(self.client.pttl(self.key + ":owner"), -1)
        with override_settings(NOMINATIM_MAX_QUEUE_SECONDS=0), patch.object(nominatim.requests, "get") as http:
            self.assertIsNone(nominatim.search_postal_code("122102", "in"))
            http.assert_not_called()

    def test_redis_failure_after_http_keeps_ownership(self):
        original = nominatim_limit._command

        def command(script, base, *args):
            if script == nominatim_limit._FINISH:
                return None
            return original(script, base, *args)

        with patch.object(nominatim_limit, "_command", side_effect=command):
            with self.assertLogs(nominatim_limit.logger, "ERROR"):
                with nominatim_limit.dispatch(PUBLIC) as permit:
                    self.assertIsNotNone(permit)
        self.assertEqual(self.client.pttl(self.key + ":owner"), -1)
        self.assertEqual(original(nominatim_limit._ACQUIRE, PUBLIC, "next"), -1)

    def test_redis_exception_prevents_http(self):
        with patch.object(self.client, "eval", side_effect=redis.ConnectionError("unavailable")), \
                patch.object(nominatim.requests, "get") as http:
            self.assertIsNone(nominatim.search_postal_code("122102", "in"))
            http.assert_not_called()
