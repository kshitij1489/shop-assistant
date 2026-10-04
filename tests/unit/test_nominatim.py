"""Site postal directory transport, caching, rate limits, and failed lookups."""
from unittest.mock import Mock, patch

import requests
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from chatbot_core.logic.cafe import nominatim, nominatim_limit

BASE = "https://nominatim.example.test"
SETTINGS = dict(
    NOMINATIM_BASE_URL=BASE, NOMINATIM_API_KEY="", NOMINATIM_TIMEOUT_SECONDS=2,
    NOMINATIM_USER_AGENT="StudioDeskTest/1.0",
)
PLACE = {
    "lat": "28.4595", "lon": "77.0266",
    "display_name": "Main Road, Sector 56, Gurugram, Haryana, 122102, India",
    "address": {
        "house_number": "4", "road": "Main Road", "suburb": "Sector 56", "city": "Gurugram",
        "state": "Haryana", "postcode": "122102", "country": "India", "country_code": "in",
    },
}


def _response(payload):
    response = Mock(status_code=200, headers={})
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


@override_settings(**SETTINGS)
class NominatimAdapterTests(SimpleTestCase):
    def setUp(self):
        self.http = self.enterContext(patch.object(nominatim.requests, "get", return_value=_response([PLACE])))

    def test_provider_failures_do_not_log_request_parameters(self):
        secret = "private-query-value"
        self.http.side_effect = requests.Timeout("timed out")
        with self.assertLogs(nominatim.logger, "WARNING") as captured:
            self.assertIsNone(nominatim.lookup_osm_object(secret))
        self.http.side_effect = None
        failed = Mock(status_code=500, headers={})
        failed.raise_for_status.side_effect = requests.HTTPError("429")
        self.http.return_value = failed
        with self.assertLogs(nominatim.logger, "WARNING") as captured_http:
            self.assertIsNone(nominatim.lookup_osm_object(secret))
        logged = " ".join(record.getMessage() + str(record.__dict__) for record in captured.records + captured_http.records)
        self.assertNotIn(secret, logged)

    @override_settings(NOMINATIM_API_KEY="osm-secret")
    def test_api_key_is_sent_and_not_logged(self):
        self.http.side_effect = requests.ConnectionError("down")
        with self.assertLogs(nominatim.logger, "WARNING") as captured:
            self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.assertEqual(self.http.call_args.kwargs["params"]["key"], "osm-secret")
        logged = " ".join(str(record.__dict__) for record in captured.records)
        self.assertNotIn("osm-secret", logged)

    def test_hosted_service_is_not_pace_limited(self):
        with patch.object(nominatim_limit, "_command") as command:
            self.assertIsNotNone(nominatim.search_postal_code("122102", "in"))
        command.assert_not_called()

    @override_settings(NOMINATIM_BASE_URL="")
    def test_missing_base_url_fails_closed(self):
        with self.assertLogs(nominatim.logger, "WARNING"):
            self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.http.assert_not_called()

    @override_settings(NOMINATIM_BASE_URL="https://user:secret@nominatim.example.test/search?q=1")
    def test_malformed_base_url_is_rejected_before_the_request(self):
        with self.assertRaises(ImproperlyConfigured):
            nominatim.search_postal_code("122102", "in")
        self.http.assert_not_called()

    @override_settings(NOMINATIM_TIMEOUT_SECONDS=0)
    def test_non_positive_timeout_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            nominatim.lookup_osm_object("N123")
        self.http.assert_not_called()


PUBLIC = "https://nominatim.openstreetmap.org"


class NominatimPaceTests(SimpleTestCase):
    def test_public_host_cannot_go_faster_than_one_request_per_second(self):
        self.assertEqual(nominatim_limit.interval_ms_for(PUBLIC), 1000)
        self.assertEqual(nominatim_limit.interval_ms_for(BASE), 0)
        with override_settings(NOMINATIM_MIN_INTERVAL_SECONDS="0.2"):
            self.assertEqual(nominatim_limit.interval_ms_for(PUBLIC), 1000)
        with override_settings(NOMINATIM_MIN_INTERVAL_SECONDS="2"):
            self.assertEqual(nominatim_limit.interval_ms_for(PUBLIC), 2000)
            self.assertEqual(nominatim_limit.interval_ms_for(BASE), 2000)

    @override_settings(NOMINATIM_BASE_URL=PUBLIC, NOMINATIM_MIN_INTERVAL_SECONDS=-1)
    def test_negative_interval_is_rejected_before_the_request(self):
        with patch.object(nominatim.requests, "get") as http, self.assertRaises(ImproperlyConfigured):
            nominatim.search_postal_code("122102", "in")
        http.assert_not_called()


@override_settings(NOMINATIM_BASE_URL=PUBLIC, NOMINATIM_API_KEY="", NOMINATIM_TIMEOUT_SECONDS=2,
                   NOMINATIM_USER_AGENT="StudioDeskTest/1.0", NOMINATIM_MAX_QUEUE_SECONDS=1,
                   NOMINATIM_MIN_INTERVAL_SECONDS="")
class PublicNominatimLimitTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache.backends.locmem import LocMemCache
        from uuid import uuid4
        self.enterContext(patch.object(nominatim, "cache", LocMemCache(uuid4().hex, {})))
        self.http = self.enterContext(patch.object(nominatim.requests, "get", return_value=_response([PLACE])))
        self.now = 0.0
        self.enterContext(patch.object(nominatim_limit.time, "monotonic", side_effect=lambda: self.now))
        self.sleep = self.enterContext(patch.object(nominatim_limit.time, "sleep", side_effect=self.advance))
        self.command = self.enterContext(patch.object(nominatim_limit, "_command", return_value=0))

    def advance(self, seconds):
        self.now += seconds

    def test_available_slot_is_sent_without_waiting(self):
        self.assertIsNotNone(nominatim.search_postal_code("122102", "in"))
        self.sleep.assert_not_called()
        self.http.assert_called_once()
        self.assertFalse(self.http.call_args.kwargs["allow_redirects"])
        self.assertEqual(self.http.call_args.args[0], PUBLIC + "/search")
        self.assertEqual([c.args[0] for c in self.command.call_args_list],
                         [nominatim_limit._ACQUIRE, nominatim_limit._FINISH])
        self.assertEqual(self.command.call_args.args[-1], 1000)

    def test_repeated_public_query_uses_cached_result_without_dispatch(self):
        first = nominatim.search_postal_code("122102", "in")
        self.command.reset_mock()
        self.assertEqual(nominatim.search_postal_code("122102", "in"), first)
        self.http.assert_called_once()
        self.command.assert_not_called()

    def test_queued_lookup_rechecks_before_sending(self):
        self.command.side_effect = [1000, 0, 0]
        self.assertIsNotNone(nominatim.search_postal_code("122102", "in"))
        self.assertEqual(self.now, 1.0)
        self.assertEqual(self.command.call_count, 3)
        self.http.assert_called_once()

    def test_new_cooldown_while_waiting_prevents_send(self):
        self.command.side_effect = [-1, 60_000]
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.http.assert_not_called()
        self.assertEqual(self.command.call_count, 2)

    def test_lookup_past_the_queue_is_not_sent(self):
        self.command.return_value = 1001
        with self.assertLogs(nominatim_limit.logger, "WARNING"):
            self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.http.assert_not_called()
        self.sleep.assert_not_called()

    def test_busy_owner_exhausts_wait_budget(self):
        self.command.return_value = -1
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.assertAlmostEqual(self.now, 1.0)
        self.http.assert_not_called()

    def test_oversleep_does_not_acquire_after_deadline(self):
        self.command.return_value = 500
        self.sleep.side_effect = lambda _: self.advance(2)
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.command.assert_called_once()
        self.http.assert_not_called()

    def test_redis_failure_does_not_send(self):
        self.command.return_value = None
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.http.assert_not_called()
        self.sleep.assert_not_called()

    def test_rate_limit_response_installs_full_cooldown(self):
        self.http.return_value = Mock(status_code=429, headers={"Retry-After": "120"})
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.assertEqual(self.command.call_args.args[0], nominatim_limit._FINISH)
        self.assertEqual(self.command.call_args.args[-1], 120_000)

    def test_missing_retry_after_uses_one_minute(self):
        self.http.return_value = Mock(status_code=429, headers={})
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.assertEqual(self.command.call_args.args[-1], 60_000)

    def test_timeout_still_releases_with_spacing(self):
        self.http.side_effect = requests.Timeout("timeout")
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.assertEqual(self.command.call_args.args[0], nominatim_limit._FINISH)
        self.assertEqual(self.command.call_args.args[-1], 1000)

    def test_redirect_cannot_create_an_unpaced_second_request(self):
        self.http.return_value = Mock(status_code=302, headers={"Location": PUBLIC + "/search"})
        self.assertIsNone(nominatim.search_postal_code("122102", "in"))
        self.http.return_value.json.assert_not_called()
        self.assertFalse(self.http.call_args.kwargs["allow_redirects"])

    def test_release_failure_is_reported(self):
        self.command.side_effect = [0, None]
        with self.assertLogs(nominatim_limit.logger, "ERROR"):
            self.assertIsNotNone(nominatim.search_postal_code("122102", "in"))


class RetryAfterTests(SimpleTestCase):
    def test_seconds(self):
        self.assertEqual(nominatim_limit._retry_after_ms("120"), 120_000)
        self.assertEqual(nominatim_limit._retry_after_ms("0"), 1000)

    def test_http_date_uses_server_date_despite_local_clock_skew(self):
        self.assertEqual(nominatim_limit._retry_after_ms(
            "Wed, 21 Oct 2015 07:30:00 GMT", "Wed, 21 Oct 2015 07:28:00 GMT"), 120_000)

    def test_invalid_header_uses_conservative_fallback(self):
        for value in (None, "", "garbage", "-1", "1.5"):
            with self.subTest(value=value):
                self.assertEqual(nominatim_limit._retry_after_ms(value), 60_000)
