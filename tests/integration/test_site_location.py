"""Location settings against OSM provider responses; no network or application DB."""
from copy import deepcopy
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from chatbot_core.models import TenantInfo
from users.models import TenantProfile
from users import site_location


CITY = {
    'city': 'Kolkata', 'state': 'West Bengal', 'country': 'India', 'country_code': 'IN',
    'city_place_id': 'N12345',
    'components': {'city_names': ['Kolkata'], 'state_names': ['inwb', 'westbengal']},
}
FORM_DATA = {
    'section': 'contact', 'street_address_1': '14 Park Street', 'street_address_2': '',
    'city': 'Kolkata', 'state': 'West Bengal', 'country': 'India',
    'city_place_id': 'N12345', 'postal_code': '700016', 'whatsapp_number': '+919876543210',
}
OSM_SETTINGS = dict(GOOGLE_MAPS_API_KEY='', PHOTON_BASE_URL='https://photon.example.test',
                    NOMINATIM_BASE_URL='https://nominatim.example.test', NOMINATIM_API_KEY='',
                    NOMINATIM_MIN_INTERVAL_SECONDS=0)
ADDRESS = {'city': 'Kolkata', 'state': 'West Bengal', 'ISO3166-2-lvl4': 'IN-WB',
           'country': 'India', 'country_code': 'in'}


def city_response():
    return [{'osm_type': 'node', 'osm_id': 12345, 'addresstype': 'city',
             'name': 'Kolkata', 'address': dict(ADDRESS)}]


def postal_response(**overrides):
    return [{'address': {**ADDRESS, 'postcode': '700016', **overrides}}]


def photon_response():
    return {'features': [{'type': 'Feature', 'properties': {
        'osm_type': 'N', 'osm_id': 12345, 'osm_key': 'place', 'osm_value': 'city',
        'name': 'Kolkata', 'state': 'West Bengal', 'country': 'India', 'countrycode': 'IN',
    }}]}


@override_settings(**OSM_SETTINGS)
class LocationProviderTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.request = self.enterContext(patch('users.site_location.requests.get'))
        self.request.return_value.status_code = 200

    def test_autocomplete_uses_photon_and_caches_results_without_google(self):
        self.request.return_value.json.return_value = photon_response()
        expected = [{'place_id': 'N12345', 'label': 'Kolkata, West Bengal, India'}]
        self.assertEqual(site_location.search_cities('kol, india'), expected)
        self.assertEqual(site_location.search_cities('kol, india'), expected)
        self.request.assert_called_once()
        self.assertEqual(self.request.call_args.args[0], 'https://photon.example.test/api/')
        self.assertEqual(self.request.call_args.kwargs['params']['layer'], 'city')
        self.assertNotIn('key', self.request.call_args.kwargs['params'])
        self.assertFalse(self.request.call_args.kwargs['allow_redirects'])

    def test_autocomplete_handles_new_type_field_deduplicates_and_filters_non_cities(self):
        data = photon_response()
        other = deepcopy(data['features'][0])
        other['properties'].update(osm_id=456, osm_key='boundary', osm_value='administrative', type='city')
        data['features'] += [deepcopy(data['features'][0]), other,
                             {'properties': {'osm_key': 'place', 'osm_value': 'country'}}, None]
        self.request.return_value.json.return_value = data
        self.assertEqual([r['place_id'] for r in site_location.search_cities('kol')], ['N12345', 'N456'])

    def test_details_resolve_selected_osm_id_through_nominatim(self):
        self.request.return_value.json.return_value = city_response()
        self.assertEqual(site_location.get_city('N12345'), CITY)
        self.assertEqual(self.request.call_args.args[0], 'https://nominatim.example.test/lookup')
        self.assertEqual(self.request.call_args.kwargs['params']['osm_ids'], 'N12345')
        self.assertEqual(self.request.call_args.kwargs['params']['accept-language'], 'en')

    def test_postal_requires_city_state_and_country(self):
        cases = [
            (postal_response(), True),
            (postal_response(city='Mumbai'), False),
            (postal_response(state='Maharashtra', **{'ISO3166-2-lvl4': 'IN-MH'}), False),
            (postal_response(country='United States', country_code='us'), False),
            (postal_response(city='Different city', county='Kolkata'), False),
        ]
        for response, expected in cases:
            with self.subTest(response=response):
                self.request.return_value.json.return_value = response
                self.assertEqual(site_location.valid_postal_code('700016', CITY), expected)
        params = self.request.call_args.kwargs['params']
        self.assertEqual(params['postalcode'], '700016')
        self.assertEqual(params['countrycodes'], 'in')
        self.assertNotIn('street', params)

    def test_missing_or_partial_postal_evidence_is_unverified(self):
        for response in ([], postal_response(postcode='700'), postal_response(city=''),
                         postal_response(state='', **{'ISO3166-2-lvl4': ''}), postal_response(postcode='700016;700017')):
            with self.subTest(response=response):
                self.request.return_value.json.return_value = response
                with self.assertRaises(site_location.PostalCodeUnverified):
                    site_location.valid_postal_code('700016', CITY)

    def test_city_without_state_accepts_postal_state_but_still_checks_city_country_and_code(self):
        city = deepcopy(CITY)
        city['state'] = ''
        city['components']['state_names'] = []
        self.request.return_value.json.return_value = postal_response()
        self.assertTrue(site_location.valid_postal_code('700016', city))
        for response in (postal_response(city='Mumbai'), postal_response(country_code='us')):
            self.request.return_value.json.return_value = response
            self.assertFalse(site_location.valid_postal_code('700016', city))
        self.request.return_value.json.return_value = postal_response(postcode='700')
        with self.assertRaises(site_location.PostalCodeUnverified):
            site_location.valid_postal_code('700016', city)

    def test_localized_alternate_and_short_names_match_postal_results(self):
        for key in ('alt_name:en', 'short_name', 'short_name:en'):
            with self.subTest(key=key):
                response = city_response()
                response[0]['namedetails'] = {key: 'Calcutta;Another alias'}
                self.request.return_value.json.return_value = response
                city = site_location.get_city('N12345')
                self.request.return_value.json.return_value = postal_response(city='Calcutta')
                self.assertTrue(site_location.valid_postal_code('700016', city))

    def test_city_place_tag_accepts_administrative_address_type_without_accepting_arbitrary_boundaries(self):
        for city_type in site_location.CITY_TYPES:
            with self.subTest(city_type=city_type):
                response = city_response()
                response[0].update(osm_type='relation', addresstype='administrative', category='place', type=city_type)
                self.request.return_value.json.return_value = response
                self.assertEqual(site_location.get_city('R12345')['city'], 'Kolkata')
        for category, kind in (('boundary', 'administrative'), ('place', 'state'), ('amenity', 'city')):
            response[0].update(category=category, type=kind)
            with self.assertRaises(site_location.InvalidCity):
                site_location.get_city('R12345')

    def test_aliases_iso_state_codes_and_alphanumeric_postal_codes(self):
        city = {'country_code': 'GB', 'components': {
            'city_names': ['London'], 'state_names': ['england', 'gbeng']}}
        self.request.return_value.json.return_value = [{'address': {
            'city': 'London', 'state': 'England', 'country_code': 'gb', 'postcode': 'SW1A 1AA'}}]
        self.assertTrue(site_location.valid_postal_code('sw1a1aa', city))
        response = city_response()
        response[0]['namedetails'] = {'name:en': 'Kolkata', 'alt_name': 'Calcutta'}
        self.request.return_value.json.return_value = response
        city = site_location.get_city('N12345')
        self.request.return_value.json.return_value = postal_response(city='Calcutta', state='Localized state name')
        self.assertTrue(site_location.valid_postal_code('700016', city))

    def test_outages_and_malformed_responses_do_not_claim_invalid_postal_code(self):
        for response in ({'error': 'bad request'}, None, [None]):
            self.request.return_value.json.return_value = response
            with self.assertRaises(site_location.LocationUnavailable):
                site_location.valid_postal_code('700016', CITY)
        self.request.side_effect = requests.Timeout('sensitive-url')
        with self.assertRaisesMessage(site_location.LocationUnavailable, 'temporarily unavailable'):
            site_location.search_cities('kol')
        with self.assertRaisesMessage(site_location.LocationUnavailable, 'temporarily unavailable'):
            site_location.get_city('N12345')

    def test_missing_configuration_and_public_nominatim_cannot_be_autocomplete(self):
        for base in ('', 'https://nominatim.openstreetmap.org', 'https://nominatim.openstreetmap.org./',
                     'https://user:secret@example.test', 'https://example.test/?key=secret'):
            with self.subTest(base=base), override_settings(PHOTON_BASE_URL=base):
                with self.assertRaises(site_location.LocationUnavailable):
                    site_location.search_cities('kol')
        with override_settings(NOMINATIM_BASE_URL=''):
            with self.assertRaisesMessage(site_location.LocationUnavailable, 'not configured'):
                site_location.get_city('N12345')
        self.request.assert_not_called()

    def test_invalid_place_or_postal_syntax_and_non_city_objects_are_rejected(self):
        for place_id in ('../anything', 'google-place-id', 'N123,W456'):
            with self.assertRaises(site_location.InvalidCity):
                site_location.get_city(place_id)
        self.assertFalse(site_location.valid_postal_code('700016|country:US', CITY))
        self.request.assert_not_called()
        response = city_response()
        response[0]['addresstype'] = 'restaurant'
        self.request.return_value.json.return_value = response
        with self.assertRaises(site_location.InvalidCity):
            site_location.get_city('N12345')
        response[0]['addresstype'] = 'city'
        response[0]['osm_id'] = 999
        with self.assertRaises(site_location.InvalidCity):
            site_location.get_city('N12345')


@override_settings(**OSM_SETTINGS, LEGACY_TENANT_SYNC_ENABLED=False)
class SiteLocationSettingsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.tenant = TenantInfo.objects.create(display_name='Location café', approval_status='APPROVED', address='Legacy address')
        self.other = TenantInfo.objects.create(display_name='Other café', address='Other address')
        self.user = get_user_model().objects.create_user(username='location-owner')
        TenantProfile.objects.create(user=self.user, tenant=self.tenant)
        self.client.force_login(self.user)
        self.url = reverse('tenant:tenant_settings')
        self.lookup_url = reverse('tenant:site_location_cities')
        self.provider = self.enterContext(patch('users.site_location.requests.get'))
        self.provider.return_value.status_code = 200

    @override_settings(PHOTON_BASE_URL='', NOMINATIM_BASE_URL='')
    def test_whatsapp_saves_without_address_or_geocoders_and_cannot_change_other_fields(self):
        response = self.client.post(self.url, {
            'section': 'whatsapp', 'whatsapp_number': '  +919876543210  ',
            'street_address_1': 'Ignored', 'address': 'Ignored', 'telegram_bot_token': 'Ignored',
        })
        self.assertRedirects(response, f'{self.url}?tab=contact')
        self.provider.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.whatsapp_number, '+919876543210')
        self.assertEqual(self.tenant.address, 'Legacy address')
        self.assertEqual(self.tenant.street_address_1, '')
        self.assertIsNone(self.tenant.telegram_bot_token)
        invalid = self.client.post(self.url, {'section': 'whatsapp', 'whatsapp_number': '9' * 21})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.context['active_tab'], 'contact')
        self.assertIn('whatsapp_number', invalid.context['whatsapp_form'].errors)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.whatsapp_number, '+919876543210')
        self.client.post(self.url, {'section': 'whatsapp', 'whatsapp_number': ''})
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.whatsapp_number, '')

    @override_settings(PHOTON_BASE_URL='', NOMINATIM_BASE_URL='')
    def test_unchanged_address_skips_lookup_and_leaves_stored_fields_alone(self):
        for name, value in FORM_DATA.items():
            if name not in ('section', 'whatsapp_number'):
                setattr(self.tenant, name, value)
        self.tenant.country_code = 'IN'
        self.tenant.save()
        response = self.client.post(self.url, {**FORM_DATA, 'whatsapp_number': 'ignored'})
        self.assertRedirects(response, f'{self.url}?tab=contact')
        self.provider.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.country_code, 'IN')
        self.assertEqual(self.tenant.address, 'Legacy address')
        self.assertIsNone(self.tenant.whatsapp_number)
        changed = self.client.post(self.url, {**FORM_DATA, 'street_address_2': 'Floor 2'})
        self.assertContains(changed, 'not configured', status_code=400)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.street_address_2, '')

    def test_save_persists_components_and_compatible_address_for_current_tenant(self):
        self.provider.return_value.json.side_effect = [city_response(), postal_response()]
        response = self.client.post(self.url, {**FORM_DATA, 'street_address_2': 'Floor 2'})
        self.assertRedirects(response, f'{self.url}?tab=contact')
        self.tenant.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.tenant.address, '14 Park Street, Floor 2, Kolkata, West Bengal, India, 700016')
        self.assertEqual(self.tenant.city_place_id, 'N12345')
        self.assertEqual(self.tenant.country_code, 'IN')
        self.assertEqual(self.tenant.postal_code, '700016')
        self.assertIsNone(self.tenant.whatsapp_number)
        self.assertEqual(self.other.address, 'Other address')
        self.assertContains(self.client.get(self.url), 'value="Floor 2"')

    def test_mismatch_preserves_database_and_submitted_values(self):
        self.provider.return_value.json.side_effect = [city_response(), postal_response(city='Mumbai')]
        response = self.client.post(self.url, FORM_DATA)
        self.assertContains(response, 'Enter a valid postal code for the city.', status_code=400)
        self.assertContains(response, 'value="14 Park Street"', status_code=400)
        self.assertContains(response, 'value="700016"', status_code=400)
        self.assertEqual(response.context['active_tab'], 'contact')
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.address, 'Legacy address')
        self.assertIsNone(self.tenant.whatsapp_number)

    def test_incomplete_osm_data_is_unverified_and_keeps_previous_address(self):
        self.provider.return_value.json.side_effect = [city_response(), postal_response(city='')]
        response = self.client.post(self.url, FORM_DATA)
        self.assertContains(response, 'We could not verify this postal code', status_code=400)
        self.assertNotContains(response, 'Enter a valid postal code', status_code=400)
        self.assertContains(response, 'value="700016"', status_code=400)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.address, 'Legacy address')

    def test_google_selection_requires_reselection_without_calling_google(self):
        response = self.client.post(self.url, {**FORM_DATA, 'city_place_id': 'google-place-id'})
        self.assertContains(response, 'Select a city from the suggestions.', status_code=400)
        self.provider.assert_not_called()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.address, 'Legacy address')

    def test_mandatory_fields_and_forged_components_are_rejected(self):
        for field in ('street_address_1', 'city', 'country', 'postal_code', 'city_place_id'):
            with self.subTest(field=field):
                response = self.client.post(self.url, {**FORM_DATA, field: ''})
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.context['location_form'].errors)
        self.provider.assert_not_called()
        for field in ('city', 'state', 'country'):
            self.provider.return_value.json.return_value = city_response()
            response = self.client.post(self.url, {**FORM_DATA, field: 'Forged'})
            self.assertEqual(response.status_code, 400)
            self.assertIn('city', response.context['location_form'].errors)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.address, 'Legacy address')

    def test_lookup_failure_is_retriable_and_does_not_save(self):
        self.provider.side_effect = requests.Timeout()
        response = self.client.post(self.url, FORM_DATA)
        self.assertContains(response, 'temporarily unavailable', status_code=400)
        self.assertNotContains(response, 'Enter a valid postal code', status_code=400)
        lookup = self.client.get(self.lookup_url, {'q': 'kol'})
        self.assertEqual(lookup.status_code, 503)
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.address, 'Legacy address')

    def test_search_and_details_authentication_and_input_bounds(self):
        self.assertEqual(self.client.get(self.lookup_url, {'q': 'k'}).json(), {'results': []})
        self.assertEqual(self.client.get(self.lookup_url, {'q': 'x' * 201}).status_code, 400)
        self.provider.assert_not_called()
        self.provider.return_value.json.return_value = city_response()
        details = self.client.get(self.lookup_url, {'place_id': 'N12345'})
        self.assertEqual(details.json()['state'], 'West Bengal')
        self.assertNotIn('test-location-key', details.content.decode())
        self.assertIn('no-store', details['Cache-Control'])
        self.tenant.is_active = False
        self.tenant.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(self.lookup_url, {'q': 'kol'}, HTTP_ACCEPT='application/json').status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get(self.lookup_url, {'q': 'kol'}).status_code, 302)

    def test_master_free_text_edit_clears_stale_components(self):
        from users.forms import TenantDirectoryForm
        self.tenant.city = 'Kolkata'
        self.tenant.city_place_id = 'N12345'
        self.tenant.save()
        prefix = f'tenant-{self.tenant.pk}'
        form = TenantDirectoryForm({
            f'{prefix}-display_name': self.tenant.display_name,
            f'{prefix}-address': 'Replacement address',
            f'{prefix}-username_{self.user.tenantprofile.pk}': self.user.username,
        }, tenant=self.tenant)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.address, 'Replacement address')
        self.assertEqual(self.tenant.city_place_id, '')
        self.assertEqual(self.tenant.city, '')
