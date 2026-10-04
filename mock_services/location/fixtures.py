"""Explicit geocoding fixtures: validation, matching and synthetic coordinates.

A successful lookup requires a fixture. Nothing here invents coordinates for an
arbitrary address; unmatched queries are a documented "no result" outcome.
"""
import hashlib
import json
import math
import re

KINDS = ('forward', 'reverse')
FIXTURE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
PINCODE = re.compile(r'(?<!\w)[1-9][0-9]{5}(?!\w)')
DEFAULT_TOLERANCE_DEGREES = 0.0005
# Synthetic coordinates stay inside one bounding box so they are recognisably
# test data and never resolve to a customer's real location.
SYNTHETIC_BOX = dict(lat=(8.0, 36.0), lng=(68.0, 97.0))
COMPONENT_TYPES = frozenset({
    'street_number', 'route', 'sublocality', 'sublocality_level_1', 'sublocality_level_2',
    'locality', 'postal_town', 'administrative_area_level_1', 'administrative_area_level_2',
    'administrative_area_level_3', 'postal_code', 'country', 'premise', 'neighborhood',
})


def normalize_text(value):
    """Case/punctuation-insensitive form used for exact address fixtures."""
    if not isinstance(value, str):
        return ''
    return ' '.join(re.sub(r'[^\w]+', ' ', value.casefold()).split())


def extract_pincode(text):
    matches = PINCODE.findall(text or '')
    return matches[0] if len(set(matches)) == 1 else None


def valid_coordinate(value, limit):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Coordinates must be finite numbers.')
    value = float(value)
    if not math.isfinite(value) or not -limit <= value <= limit:
        raise ValueError('Coordinates must be finite numbers within range.')
    return value


def parse_latlng(text):
    parts = (text or '').split(',')
    if len(parts) != 2:
        raise ValueError('latlng must be "latitude,longitude".')
    try:
        lat, lng = float(parts[0]), float(parts[1])
    except ValueError as exc:
        raise ValueError('latlng must be "latitude,longitude".') from exc
    return valid_coordinate(lat, 90), valid_coordinate(lng, 180)


def synthetic_coordinates(key):
    """Deterministic, account-independent coordinates for a fixture key."""
    digest = hashlib.sha256(key.encode()).digest()
    lat_unit = int.from_bytes(digest[:4], 'big') / 0xFFFFFFFF
    lng_unit = int.from_bytes(digest[4:8], 'big') / 0xFFFFFFFF
    (lat_low, lat_high), (lng_low, lng_high) = SYNTHETIC_BOX['lat'], SYNTHETIC_BOX['lng']
    return (round(lat_low + (lat_high - lat_low) * lat_unit, 6),
            round(lng_low + (lng_high - lng_low) * lng_unit, 6))


def _validate_match(kind, match):
    if not isinstance(match, dict) or not match:
        raise ValueError('Fixture match must be a nonempty object.')
    if kind == 'forward':
        if set(match) - {'address', 'address_terms', 'postal_code'}:
            raise ValueError('Forward fixtures match on address, address_terms and/or postal_code.')
        if 'postal_code' in match and not re.fullmatch(r'[1-9][0-9]{5}', str(match['postal_code'])):
            raise ValueError('postal_code must be a six-digit pincode.')
        if 'address' in match and not normalize_text(match['address']):
            raise ValueError('address must be nonempty text.')
        if 'address_terms' in match:
            terms = match['address_terms']
            if ('address' in match or not isinstance(terms, list) or not terms
                    or any(not isinstance(term, str) or not normalize_text(term) for term in terms)):
                raise ValueError('address_terms requires nonempty component strings and excludes address.')
        return {key: list(value) if key == 'address_terms' else str(value) for key, value in match.items()}
    if set(match) - {'latitude', 'longitude', 'tolerance_degrees'} or not {'latitude', 'longitude'} <= set(match):
        raise ValueError('Reverse fixtures match on latitude, longitude and optional tolerance_degrees.')
    tolerance = match.get('tolerance_degrees', DEFAULT_TOLERANCE_DEGREES)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or not 0 <= tolerance <= 1:
        raise ValueError('tolerance_degrees must be between 0 and 1.')
    return dict(latitude=valid_coordinate(match['latitude'], 90),
                longitude=valid_coordinate(match['longitude'], 180), tolerance_degrees=float(tolerance))


def _validate_components(components):
    if not isinstance(components, dict) or set(components) - COMPONENT_TYPES:
        raise ValueError('components must map known address component types to text.')
    if any(not isinstance(value, str) or not value.strip() for value in components.values()):
        raise ValueError('components values must be nonempty text.')
    if 'postal_code' in components and not re.fullmatch(r'[1-9][0-9]{5}', components['postal_code']):
        raise ValueError('components.postal_code must be a six-digit pincode.')
    return {key: value.strip() for key, value in components.items()}


def _validate_result(kind, match, result):
    if not isinstance(result, dict) or set(result) - {'formatted_address', 'latitude', 'longitude', 'components', 'partial_match'}:
        raise ValueError('Fixture result has unknown fields.')
    formatted = result.get('formatted_address')
    if not isinstance(formatted, str) or not formatted.strip():
        raise ValueError('formatted_address must be nonempty text.')
    partial = result.get('partial_match', False)
    if type(partial) is not bool:
        raise ValueError('partial_match must be a boolean.')
    has_lat, has_lng = 'latitude' in result, 'longitude' in result
    if has_lat != has_lng:
        raise ValueError('latitude and longitude must be supplied together.')
    if has_lat:
        lat, lng = valid_coordinate(result['latitude'], 90), valid_coordinate(result['longitude'], 180)
    else:
        lat, lng = synthetic_coordinates(kind + ':' + json.dumps(match, sort_keys=True))
    return dict(formatted_address=formatted.strip(), latitude=lat, longitude=lng,
                components=_validate_components(result.get('components', {})), partial_match=partial)


def validate_fixture(fixture):
    """Return the canonical fixture or raise ValueError describing the problem."""
    if not isinstance(fixture, dict) or set(fixture) - {'fixture_id', 'kind', 'match', 'result'}:
        raise ValueError('Fixture must contain fixture_id, kind, match and result only.')
    fixture_id = fixture.get('fixture_id')
    if not isinstance(fixture_id, str) or not FIXTURE_ID.fullmatch(fixture_id):
        raise ValueError('fixture_id must use the safe identifier alphabet.')
    kind = fixture.get('kind')
    if kind not in KINDS:
        raise ValueError('kind must be forward or reverse.')
    match = _validate_match(kind, fixture.get('match'))
    return dict(fixture_id=fixture_id, kind=kind, match=match, result=_validate_result(kind, match, fixture.get('result')))


def match_forward(fixtures, address):
    """Prefer exact address, then supplied components, then pincode; reject ties."""
    text, pincode = normalize_text(address), extract_pincode(address)
    exact, by_components, by_pincode = [], [], []
    for fixture in fixtures:
        if fixture['kind'] != 'forward':
            continue
        match = fixture['match']
        if 'postal_code' in match and match['postal_code'] != pincode:
            continue
        if 'address' in match:
            if normalize_text(match['address']) == text:
                exact.append(fixture)
        elif 'address_terms' in match:
            # Match complete component token sequences, so Flat 2 cannot match
            # Flat 22. Extra verified jurisdiction text does not affect identity.
            tokens = text.split()
            terms = [normalize_text(term).split() for term in match['address_terms']]
            if all(any(tokens[i:i + len(term)] == term for i in range(len(tokens) - len(term) + 1)) for term in terms):
                by_components.append(fixture)
        else:
            by_pincode.append(fixture)
    return _single(exact or by_components or by_pincode)


def match_reverse(fixtures, lat, lng):
    candidates = [fixture for fixture in fixtures if fixture['kind'] == 'reverse'
                  and abs(fixture['match']['latitude'] - lat) <= fixture['match']['tolerance_degrees']
                  and abs(fixture['match']['longitude'] - lng) <= fixture['match']['tolerance_degrees']]
    return _single(candidates)


def _single(candidates):
    if len(candidates) == 1:
        return candidates[0], None
    return None, ('ambiguous_fixture' if candidates else None)
