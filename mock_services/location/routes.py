"""Path routing for the location emulator; the loopback server owns transport.

Lookup:  GET  /v1/location/accounts/{account}/geocode?address=...
         GET  /v1/location/accounts/{account}/reverse?latlng=lat,lng
Control: POST /admin/location/accounts/{account}/fixtures        {"fixtures": [...]}
         GET  /admin/location/accounts/{account}/fixtures
         POST /admin/location/accounts/{account}/controls        {"geocoding": {...}, ...}
         POST /admin/location/accounts/{account}/controls/queue  {"service": ..., "outcome": ..., "postal_code": ...}
         GET  /admin/location/accounts/{account}/state
         POST /admin/location/accounts/{account}/reset           {}
"""
from contextlib import closing
from urllib.parse import parse_qs

from .engine import Decision
from .fixtures import parse_latlng
from .store import LocationStore, validate_account

LOOKUP_PREFIX = ('v1', 'location', 'accounts')
ADMIN_PREFIX = ('admin', 'location', 'accounts')


def is_location_path(parts):
    return tuple(parts[:3]) in (LOOKUP_PREFIX, ADMIN_PREFIX)


def route(emulator, method, parts, query, read_body):
    """Return a Decision or raise LookupError (404) / ValueError (400)."""
    if len(parts) < 5:
        raise LookupError('unknown_endpoint')
    account, action = validate_account(parts[3]), parts[4:]
    if tuple(parts[:3]) == LOOKUP_PREFIX:
        return _lookup(emulator, method, account, action, parse_qs(query))
    return _admin(emulator, method, account, action, read_body)


def _lookup(emulator, method, account, action, params):
    if method != 'GET' or len(action) != 1:
        raise LookupError('unknown_endpoint')
    if action[0] == 'geocode':
        address = params.get('address', [''])[0]
        if not address.strip():
            raise ValueError('address is required.')
        return emulator.lookup(account, 'geocoding', address.strip())
    if action[0] == 'reverse':
        return emulator.lookup(account, 'reverse_geocoding', parse_latlng(params.get('latlng', [''])[0]))
    raise LookupError('unknown_endpoint')


def _admin(emulator, method, account, action, read_body):
    with closing(LocationStore(emulator.database, account)) as store:
        if action == ['fixtures'] and method == 'POST':
            fixtures = store.replace_fixtures(read_body()['fixtures'])
            return Decision(200, {'account': account, 'fixtures': fixtures})
        if action == ['fixtures'] and method == 'GET':
            return Decision(200, {'account': account, 'fixtures': store.fixtures()})
        if action == ['controls'] and method == 'POST':
            return Decision(200, {'account': account, 'controls': store.replace_controls(read_body())})
        if action == ['controls', 'queue'] and method == 'POST':
            body = read_body()
            entry = {key: body[key] for key in ('outcome', 'postal_code') if key in body}
            rule = store.queue_outcome(body['service'], entry)
            return Decision(200, {'account': account, 'service': body['service'], 'control': rule})
        if action == ['reset'] and method == 'POST':
            read_body()
            store.reset()
            return Decision(200, {'account': account, 'reset': True})
    if action == ['state'] and method == 'GET':
        return Decision(200, emulator.state(account))
    raise LookupError('unknown_endpoint')
