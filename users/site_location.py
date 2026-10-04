"""OSM city autocomplete via Photon and component verification via Nominatim."""
import hashlib
import re
import unicodedata
from urllib.parse import urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured

from chatbot_core.logic.cafe import nominatim

CITY_TYPES = ('city', 'town', 'village', 'municipality')
OSM_ID = r'[NWR][1-9][0-9]{0,19}'


class LocationUnavailable(Exception):
    pass


class InvalidCity(ValueError):
    pass


class PostalCodeUnverified(ValueError):
    pass


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def search_cities(query):
    """Only Photon receives typing requests; Nominatim is never autocomplete."""
    base = _text(getattr(settings, 'PHOTON_BASE_URL', '')).rstrip('/')
    if not base:
        raise LocationUnavailable('City search is not configured. Please contact support.')
    parsed = urlsplit(base)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.hostname.lower().rstrip('.') == 'nominatim.openstreetmap.org'):
        raise LocationUnavailable('City search is not configured correctly. Please contact support.')
    key = 'site-city:photon:' + hashlib.sha256(f'{base}\n{query.casefold()}'.encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = requests.get(
            f'{base}/api/', params={'q': query, 'layer': 'city', 'lang': 'en', 'limit': 10},
            timeout=(3.05, 8), headers={'User-Agent': 'StudioDesk/1.0 (site-city search)',
                                      'Accept': 'application/json'}, allow_redirects=False,
        )
        response.raise_for_status()
        if 300 <= response.status_code < 400:
            raise ValueError('Redirect refused')
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('features'), list):
            raise ValueError('Invalid search response')
    except (requests.RequestException, ValueError) as exc:
        raise LocationUnavailable('City search is temporarily unavailable. Please try again.') from exc
    results, seen = [], set()
    for feature in data['features']:
        props = feature.get('properties') if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        if not (props.get('type') == 'city' or (
                props.get('osm_key') == 'place' and props.get('osm_value') in CITY_TYPES)):
            continue
        osm_type = {'node': 'N', 'way': 'W', 'relation': 'R'}.get(props.get('osm_type'), props.get('osm_type'))
        place_id = f'{osm_type}{props.get("osm_id", "")}'
        name, country = _text(props.get('name')), _text(props.get('country'))
        if not re.fullmatch(OSM_ID, place_id) or not name or not country or place_id in seen:
            continue
        seen.add(place_id)
        label = ', '.join(part for part in (name, _text(props.get('state')), country) if part)
        results.append({'place_id': place_id, 'label': label})
    cache.set(key, results[:10], timeout=300)
    return results[:10]


def _nominatim_lookup(function, *args):
    if not _text(getattr(settings, 'NOMINATIM_BASE_URL', '')):
        raise LocationUnavailable('Location validation is not configured. Please contact support.')
    try:
        data = function(*args)
    except ImproperlyConfigured as exc:
        raise LocationUnavailable('Location validation is not configured correctly. Please contact support.') from exc
    if not isinstance(data, list) or any(not isinstance(place, dict) for place in data):
        raise LocationUnavailable('Location lookup is temporarily unavailable. Please try again.')
    return data


def _first(address, keys):
    return next((_text(address.get(key)) for key in keys if _text(address.get(key))), '')


def _normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum())


def _state_names(address):
    # ISO subdivision codes are stable even when a provider changes language.
    return {_normalize(value) for value in (
        _first(address, ('state', 'region')), _text(address.get('ISO3166-2-lvl4')),
    ) if value}


def get_city(place_id):
    if not re.fullmatch(OSM_ID, place_id):
        raise InvalidCity('Select a city from the suggestions.')
    places = _nominatim_lookup(nominatim.lookup_osm_object, place_id)
    if len(places) != 1:
        raise InvalidCity('This city could not be found. Select another suggestion.')
    place = places[0]
    osm_type = {'node': 'N', 'way': 'W', 'relation': 'R'}.get(place.get('osm_type'), '')
    city_type = place.get('addresstype')
    if city_type not in CITY_TYPES and place.get('category') == 'place' and place.get('type') in CITY_TYPES:
        city_type = place['type']
    if f'{osm_type}{place.get("osm_id", "")}' != place_id or city_type not in CITY_TYPES:
        raise InvalidCity('Select a city from the suggestions.')
    address = place.get('address')
    if not isinstance(address, dict):
        raise InvalidCity('This city has incomplete location details. Select another result.')
    city = (_text(address.get(city_type)) or _text(place.get('name'))
            or _first(address, CITY_TYPES))
    state = _first(address, ('state', 'region'))
    country = _text(address.get('country'))
    country_code = _text(address.get('country_code')).upper()
    if not city or not country or not re.fullmatch('[A-Z]{2}', country_code):
        raise InvalidCity('This city has incomplete location details. Select another result.')
    aliases = {city}
    names = place.get('namedetails', {})
    if isinstance(names, dict):
        for key, value in names.items():
            if (key in ('name', 'official_name', 'alt_name', 'short_name')
                    or key.startswith(('name:', 'official_name:', 'alt_name:', 'short_name:'))):
                aliases.update(name.strip() for name in _text(value).split(';') if name.strip())
    return {'city': city, 'state': state, 'country': country, 'country_code': country_code,
            'city_place_id': place_id,
            'components': {'city_names': sorted(aliases), 'state_names': sorted(_state_names(address))}}


def valid_postal_code(postal_code, city):
    """Require positive postal/city/state/country evidence; distinguish missing data."""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 -]{0,19}', postal_code):
        return False
    results = _nominatim_lookup(nominatim.search_postal_code, postal_code, city['country_code'])
    selected_cities = {_normalize(name) for name in city['components']['city_names']}
    selected_states = set(city['components']['state_names'])
    mismatch = False
    for place in results:
        address = place.get('address')
        if not isinstance(address, dict):
            continue
        # Nominatim may broaden a search: only the complete requested code is evidence.
        if _normalize(_text(address.get('postcode'))) != _normalize(postal_code):
            continue
        found_city = _normalize(_first(address, CITY_TYPES))
        found_country = _text(address.get('country_code')).upper()
        found_states = _state_names(address)
        if not found_city or not found_country or selected_states and not found_states:
            continue
        if (found_city in selected_cities and found_country == city['country_code']
                and (not selected_states or selected_states & found_states)):
            return True
        mismatch = True
    if mismatch:
        return False
    raise PostalCodeUnverified('We could not verify this postal code for the selected city. Check the code and try again.')
