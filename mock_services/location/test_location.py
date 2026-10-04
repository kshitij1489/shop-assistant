"""Location emulator component tests over real loopback HTTP and SQLite."""
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from ..catalog import load_catalog
from ..client import MockClient
from ..controls import ProviderControls, Unsupported
from ..server import make_server
from .fixtures import match_forward, synthetic_coordinates, validate_fixture

HOME = dict(fixture_id='home', kind='forward', match={'postal_code': '122102'},
            result=dict(formatted_address='Sector 56, Gurugram, Haryana 122102, India',
                        components={'sublocality_level_1': 'Sector 56', 'locality': 'Gurugram',
                                    'administrative_area_level_1': 'Haryana', 'postal_code': '122102',
                                    'country': 'India'}))
EXACT = dict(fixture_id='exact', kind='forward', match={'address': 'Flat 4, Main Road, Delhi, 110001'},
             result=dict(formatted_address='Main Road, New Delhi, Delhi 110001, India', latitude=28.6, longitude=77.2,
                         components={'route': 'Main Road', 'locality': 'New Delhi', 'postal_code': '110001'}))
PARTIAL = dict(fixture_id='partial', kind='forward', match={'postal_code': '110002'},
               result=dict(formatted_address='Somewhere, Delhi, India', partial_match=True, components={}))
PIN = dict(fixture_id='pin', kind='reverse', match={'latitude': 28.1, 'longitude': 77.1},
           result=dict(formatted_address='Main Road, Delhi 110001, India',
                       components={'route': 'Main Road', 'locality': 'Delhi', 'postal_code': '110001'}))


class EmulatorServer:
    def __init__(self, database, port=0):
        self.server = make_server(('127.0.0.1', port), database, load_catalog())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.client = MockClient('http://127.0.0.1:%s' % self.port, timeout=5)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class LocationEmulatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / 'provider.sqlite3'
        self.http = EmulatorServer(self.database)
        self.addCleanup(self.http.stop)
        self.client = self.http.client
        self.controls = ProviderControls(self.client)
        self.controls.seed_location_fixtures('tenant-a', [HOME, EXACT, PARTIAL, PIN])

    def geocode(self, account, address, client=None):
        return (client or self.client).request('GET', '/v1/location/accounts/%s/geocode?%s' % (account, urlencode({'address': address})))

    def reverse(self, account, lat, lng):
        return self.client.request('GET', '/v1/location/accounts/%s/reverse?latlng=%s,%s' % (account, lat, lng))

    def test_success_requires_fixture_and_returns_deterministic_synthetic_geometry(self):
        result = self.geocode('tenant-a', 'Flat 9, Sector 56, Gurugram 122102')
        self.assertEqual((result['status'], result['fixture_id']), ('OK', 'home'))
        self.assertEqual(result['result']['components']['postal_code'], '122102')
        expected = synthetic_coordinates('forward:{"postal_code": "122102"}')
        self.assertEqual((result['result']['latitude'], result['result']['longitude']), expected)
        self.assertEqual(self.geocode('tenant-a', 'Flat 9, Sector 56, Gurugram 122102')['result'], result['result'])
        # Explicit coordinates are honoured; the exact-text match wins over pincode-only fixtures.
        exact = self.geocode('tenant-a', 'flat 4  main road, delhi 110001')
        self.assertEqual((exact['fixture_id'], exact['result']['latitude']), ('exact', 28.6))
        missing = self.geocode('tenant-a', 'Unknown Street, Nowhere 560001')
        self.assertEqual((missing['status'], missing['result'], missing['reason']), ('ZERO_RESULTS', None, 'no_fixture'))
        partial = self.geocode('tenant-a', 'Anywhere 110002')
        self.assertTrue(partial['result']['partial_match'])

    def test_reverse_geocoding_matches_within_tolerance_only(self):
        pin = self.reverse('tenant-a', 28.1001, 77.0999)
        self.assertEqual((pin['status'], pin['fixture_id']), ('OK', 'pin'))
        self.assertEqual(pin['result']['components']['route'], 'Main Road')
        self.assertEqual(self.reverse('tenant-a', 28.2, 77.1)['status'], 'ZERO_RESULTS')
        with self.assertRaises(HTTPError) as caught:
            self.reverse('tenant-a', 91, 77.1)
        self.assertEqual(caught.exception.code, 400)

    def test_queued_outcomes_are_consumed_in_order_and_per_account(self):
        self.controls.seed_location_fixtures('tenant-b', [HOME])
        self.controls.apply_lookup_control('tenant-a', 'geocoding', 'unavailable')
        self.controls.apply_lookup_control('tenant-a', 'geocoding', 'postal_mismatch', postal_code='110001')
        # Another account's lookup must not consume tenant-a's queued faults.
        self.assertEqual(self.geocode('tenant-b', 'Sector 56, 122102')['status'], 'OK')
        with self.assertRaises(HTTPError) as caught:
            self.geocode('tenant-a', 'Sector 56, 122102')
        self.assertEqual(caught.exception.code, 503)
        mismatch = self.geocode('tenant-a', 'Sector 56, 122102')
        self.assertEqual(mismatch['result']['components']['postal_code'], '110001')
        self.assertEqual(self.geocode('tenant-a', 'Sector 56, 122102')['status'], 'OK')
        state = self.controls.location_state('tenant-a')
        self.assertEqual(state['controls']['geocoding']['next'], [])
        self.assertEqual(state['counters']['geocoding.unavailable'], 1)
        self.assertEqual(state['counters']['geocoding.postal_mismatch'], 1)
        self.assertEqual([r['outcome'] for r in state['requests']], ['unavailable', 'postal_mismatch', 'success'])
        self.assertTrue(all('Sector' not in str(r) for r in state['requests']))
        self.assertEqual(self.controls.location_state('tenant-b')['counters'], {'geocoding.requests': 1, 'geocoding.success': 1})

    def test_sticky_default_and_timeout_are_observed_by_a_real_client(self):
        self.controls.set_location_default('tenant-a', 'reverse_geocoding', 'unavailable')
        for _ in range(2):
            with self.assertRaises(HTTPError):
                self.reverse('tenant-a', 28.1, 77.1)
        self.client.request('POST', '/admin/location/accounts/tenant-a/controls',
                            {'geocoding': {'next': [{'outcome': 'timeout'}], 'timeout_hold_ms': 400}})
        quick = MockClient(self.client.base_url, timeout=0.15)
        with self.assertRaises((URLError, TimeoutError, OSError)):
            self.geocode('tenant-a', 'Sector 56, 122102', client=quick)
        self.assertEqual(self.geocode('tenant-a', 'Sector 56, 122102')['status'], 'OK')
        outcomes = [r['outcome'] for r in self.controls.location_state('tenant-a')['requests']]
        self.assertEqual(outcomes, ['unavailable', 'unavailable', 'timeout', 'success'])

    def test_fixtures_and_queues_survive_restart_and_reseeding_is_idempotent(self):
        self.controls.apply_lookup_control('tenant-a', 'geocoding', 'unavailable')
        before = self.controls.location_state('tenant-a')
        # Re-seeding identical fixtures is a no-op for state and leaves the queue intact.
        self.controls.seed_location_fixtures('tenant-a', [HOME, EXACT, PARTIAL, PIN])
        self.assertEqual(self.controls.location_state('tenant-a'), before)
        self.http.stop()
        self.http = EmulatorServer(self.database)
        self.controls = ProviderControls(self.http.client)
        self.client = self.http.client
        state = self.controls.location_state('tenant-a')
        self.assertEqual([f['fixture_id'] for f in state['fixtures']], ['exact', 'home', 'partial', 'pin'])
        self.assertEqual(state['controls']['geocoding']['next'], [{'outcome': 'unavailable'}])
        with self.assertRaises(HTTPError):
            self.geocode('tenant-a', 'Sector 56, 122102')
        self.controls.reset_location('tenant-a')
        self.assertEqual(self.controls.location_state('tenant-a')['fixtures'], [])
        self.assertEqual(self.geocode('tenant-a', 'Sector 56, 122102')['reason'], 'no_fixture')

    def test_invalid_fixtures_controls_and_accounts_are_rejected(self):
        bad_fixtures = [
            [dict(HOME, fixture_id='bad id')], [dict(HOME, kind='nearby')], [dict(HOME, match={'postal_code': '12'})],
            [dict(HOME, result=dict(HOME['result'], latitude=91, longitude=0))],
            [dict(HOME, result=dict(HOME['result'], components={'unknown_type': 'x'}))],
            [HOME, dict(HOME)], [dict(HOME, result=dict(HOME['result'], latitude=28.0))],
        ]
        for fixtures in bad_fixtures:
            with self.subTest(fixtures=fixtures), self.assertRaises(HTTPError) as caught:
                self.controls.seed_location_fixtures('tenant-a', fixtures)
            self.assertEqual(caught.exception.code, 400)
        for body in [{'geocoding': {'next': [{'outcome': 'postal_mismatch'}]}}, {'coverage': {}},
                     {'geocoding': {'delay_ms': -1}}, {'geocoding': {'default': 'explode'}}]:
            with self.subTest(body=body), self.assertRaises(HTTPError) as caught:
                self.client.request('POST', '/admin/location/accounts/tenant-a/controls', body)
            self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(HTTPError) as caught:
            self.client.request('GET', '/v1/location/accounts/bad%20slug/geocode?address=x')
        self.assertEqual(caught.exception.code, 400)
        with self.assertRaises(Unsupported):
            self.controls.apply_lookup_control('tenant-a', 'coverage', 'unavailable')
        with self.assertRaises(Unsupported):
            self.controls.apply_lookup_control('tenant-a', 'classification', 'timeout')
        # Fixture state is unchanged after every rejected write.
        self.assertEqual(len(self.controls.location_state('tenant-a')['fixtures']), 4)

    def test_ambiguous_fixtures_do_not_guess(self):
        twin = dict(HOME, fixture_id='twin')
        fixtures = [validate_fixture(HOME), validate_fixture(twin)]
        self.assertEqual(match_forward(fixtures, 'Sector 56, 122102'), (None, 'ambiguous_fixture'))
        self.controls.seed_location_fixtures('tenant-c', [HOME, twin])
        self.assertEqual(self.geocode('tenant-c', 'Sector 56, 122102')['reason'], 'ambiguous_fixture')

    def test_replace_controls_merges_services_and_rejects_postal_code_on_success(self):
        self.controls.set_location_default('tenant-a', 'geocoding', 'postal_mismatch', postal_code='110001')
        self.controls.set_location_default('tenant-a', 'reverse_geocoding', 'unavailable')
        state = self.controls.location_state('tenant-a')
        self.assertEqual(state['controls']['geocoding']['default'],
                         {'outcome': 'postal_mismatch', 'postal_code': '110001'})
        self.assertEqual(state['controls']['reverse_geocoding']['default'], {'outcome': 'unavailable'})
        # Setting one service must leave the other intact.
        self.controls.set_location_default('tenant-a', 'geocoding', 'success')
        state = self.controls.location_state('tenant-a')
        self.assertEqual(state['controls']['geocoding']['default'], {'outcome': 'success'})
        self.assertEqual(state['controls']['reverse_geocoding']['default'], {'outcome': 'unavailable'})
        # Sticky postal_mismatch remains until restored.
        self.controls.set_location_default('tenant-a', 'geocoding', 'postal_mismatch', postal_code='122011')
        for _ in range(3):
            mismatch = self.geocode('tenant-a', 'Sector 56, 122102')
            self.assertEqual(mismatch['result']['components']['postal_code'], '122011')
        with self.assertRaises(HTTPError) as caught:
            self.client.request('POST', '/admin/location/accounts/tenant-a/controls',
                                {'geocoding': {'default': {'outcome': 'success', 'postal_code': '122102'}}})
        self.assertEqual(caught.exception.code, 400)
        # Success restore is postal_code-free; the fixture supplies 122102.
        self.controls.set_location_default('tenant-a', 'geocoding', 'success')
        restored = self.geocode('tenant-a', 'Sector 56, 122102')
        self.assertEqual(restored['result']['components']['postal_code'], '122102')


if __name__ == '__main__':
    unittest.main()
