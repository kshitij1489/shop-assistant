"""Resolve one lookup: consume the account's control, match fixtures, log evidence."""
import hashlib
from contextlib import closing
from dataclasses import dataclass

from .fixtures import extract_pincode, match_forward, match_reverse, normalize_text, synthetic_coordinates
from .store import OUTCOMES, SERVICES, LocationStore


@dataclass(frozen=True)
class Decision:
    status: int
    payload: dict
    wait_seconds: float = 0.0


def _query_digest(text):
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()[:16]


def _mismatch_result(matched, postal_code):
    """A mismatching pincode needs a successful-looking result; derive one explicitly."""
    if matched:
        result = dict(matched['result'], components=dict(matched['result']['components']))
    else:
        lat, lng = synthetic_coordinates('postal_mismatch:' + postal_code)
        result = dict(formatted_address='Synthetic locality, ' + postal_code + ', India', latitude=lat,
                      longitude=lng, components={'country': 'India'}, partial_match=False)
    result['components']['postal_code'] = postal_code
    return result


class LocationEmulator:
    def __init__(self, database):
        self.database = database
        with closing(LocationStore(database, 'schema-init')):
            pass

    def lookup(self, account, service, query):
        """query is an address string (geocoding) or a (lat, lng) tuple (reverse)."""
        if service not in SERVICES:
            raise ValueError('Unknown location service.')
        with closing(LocationStore(self.database, account)) as store:
            store.db.execute('BEGIN IMMEDIATE')
            try:
                decision = self._resolve(store, service, query)
                store.db.commit()
            except BaseException:
                store.db.rollback()
                raise
        return decision

    def _resolve(self, store, service, query):
        entry, delay_ms, hold_ms = store.take_outcome(service)
        matched, reason = self._match(store.fixtures(), service, query)
        record = dict(service=service, outcome=entry['outcome'], fixture_id=matched['fixture_id'] if matched else None,
                      postal_code=extract_pincode(query) if service == 'geocoding' else None,
                      query_digest=_query_digest(query if service == 'geocoding' else '%s,%s' % query))
        if entry['outcome'] == 'unavailable':
            store.record(dict(record, http_status=503))
            return Decision(503, {'error': 'provider_unavailable'}, delay_ms / 1000)
        if entry['outcome'] == 'timeout':
            store.record(dict(record, http_status=200, reason='held_response'))
            return Decision(200, {'status': 'ZERO_RESULTS', 'result': None, 'reason': 'held_response'}, hold_ms / 1000)
        if entry['outcome'] == 'postal_mismatch':
            result = _mismatch_result(matched, entry['postal_code'])
            store.record(dict(record, http_status=200, mismatch_postal_code=entry['postal_code']))
            return Decision(200, {'status': 'OK', 'result': result, 'fixture_id': record['fixture_id']}, delay_ms / 1000)
        if matched is None:
            reason = reason or 'no_fixture'
            store.record(dict(record, http_status=200, reason=reason))
            return Decision(200, {'status': 'ZERO_RESULTS', 'result': None, 'reason': reason}, delay_ms / 1000)
        store.record(dict(record, http_status=200, partial_match=matched['result']['partial_match']))
        return Decision(200, {'status': 'OK', 'result': matched['result'], 'fixture_id': matched['fixture_id']}, delay_ms / 1000)

    @staticmethod
    def _match(fixtures, service, query):
        if service == 'geocoding':
            return match_forward(fixtures, query)
        return match_reverse(fixtures, *query)

    def state(self, account):
        with closing(LocationStore(self.database, account)) as store:
            requests = store.requests()
            counters = {}
            for item in requests:
                counters[item['service'] + '.requests'] = counters.get(item['service'] + '.requests', 0) + 1
                key = item['service'] + '.' + item['outcome']
                counters[key] = counters.get(key, 0) + 1
            return dict(account=account, supported_outcomes=list(OUTCOMES),
                        fixtures=[dict(fixture_id=f['fixture_id'], kind=f['kind'], match=f['match']) for f in store.fixtures()],
                        controls=store.controls(), counters=counters, requests=requests)
