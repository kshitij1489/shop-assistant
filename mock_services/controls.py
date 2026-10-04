"""Provider-side controls for an evaluation runner's `ScenarioControls` adapter.

Typed operations from `evaluate/contracts/models.py` (`LookupControl`,
`PaymentControl`) are accepted as plain field values so this module stays
standard-library only. The runner resolves `target: active_order` to a Studio
Desk payment UUID from application state before calling `apply_payment_control`.
Anything outside `CAPABILITIES` is refused here, so the runner can block before
any mutation instead of guessing.
"""
from urllib.error import HTTPError
from urllib.parse import quote
from uuid import UUID

from .location.store import validate_account

CAPABILITIES = frozenset({
    'address_stubs', 'fake_payment_adapter',
    'lookup_control.geocoding', 'lookup_control.reverse_geocoding',
    'payment_control.capture', 'payment_control.fail', 'payment_control.cancel',
    'payment_control.timeout_creation',
})
UNSUPPORTED = {
    'lookup_control.coverage': 'Delivery coverage is tenant configuration (serviceable_pincodes), not a geocoder outcome.',
    'lookup_control.classification': 'Intent classification is an application LLM boundary; the provider cannot fault it.',
    'payment_control.restore_and_reconcile': 'Runner-owned: clear this account\'s faults, then run Studio Desk reconcile_commerce.',
    'payment.refund': 'The mock payment provider does not execute refunds; refund observations stay at zero.',
    'order.post_acceptance': 'POS orders are accepted only; preparing/dispatched/delivered/rejected transitions are not emulated.',
    'menu.global_catalog_fault': 'GET /v1/menu has no account; its faults are shared, so the runner must serialize them.',
}
_PAYMENT_ACTIONS = {'capture': 'capture', 'fail': 'fail', 'cancel': 'cancel'}


class Unsupported(ValueError):
    """The requested control is documented as unsupported by this provider."""


class ProviderControls:
    """Thin HTTP client over the loopback simulator's control endpoints."""

    def __init__(self, client):
        self.client = client

    @staticmethod
    def capabilities():
        return CAPABILITIES

    # Location -----------------------------------------------------------------
    @staticmethod
    def _location(account, suffix):
        return '/admin/location/accounts/' + quote(validate_account(account), safe='') + suffix

    def seed_location_fixtures(self, account, fixtures):
        """Replace the account's fixtures. Re-seeding identical input is idempotent."""
        return self.client.request('POST', self._location(account, '/fixtures'), {'fixtures': list(fixtures)})

    def set_location_default(self, account, service, outcome, postal_code=None):
        """Sticky outcome, e.g. `address_lookup: unavailable` for a whole scenario."""
        entry = _lookup_entry(service, outcome, postal_code)
        return self.client.request('POST', self._location(account, '/controls'), {service: {'default': entry}})

    def apply_lookup_control(self, account, service, outcome, postal_code=None):
        """Queue one LookupControl outcome for the account's next lookup of that service."""
        entry = _lookup_entry(service, outcome, postal_code)
        return self.client.request('POST', self._location(account, '/controls/queue'), dict(entry, service=service))

    def location_state(self, account):
        return self.client.request('GET', self._location(account, '/state'))

    def reset_location(self, account):
        return self.client.request('POST', self._location(account, '/reset'), {})

    # Payment / POS ------------------------------------------------------------
    @staticmethod
    def _account(connection):
        return '/admin/accounts/' + str(UUID(connection))

    def configure_faults(self, connection, rules):
        """Account-scoped `payment`/`pos`/`menu` fault rules; `{}` clears them."""
        return self.client.request('POST', self._account(connection) + '/faults', rules)

    def apply_payment_control(self, connection, operation, *, payment_id=None, amount_minor=None, currency='INR'):
        """Execute one PaymentControl against the connection's mock account."""
        key = 'payment_control.' + str(operation)
        if key in UNSUPPORTED:
            raise Unsupported(UNSUPPORTED[key])
        if key not in CAPABILITIES:
            raise Unsupported('Unknown payment control operation.')
        if operation == 'timeout_creation':
            return self.configure_faults(connection, {'payment': {'timeout_after_commit_next': 1}})
        if payment_id is None:
            raise ValueError('capture/fail/cancel require the Studio Desk payment UUID of the active order.')
        body = {}
        if operation == 'capture':
            if amount_minor is None:
                raise ValueError('capture requires the expected amount_minor.')
            body = {'amount_minor': amount_minor, 'currency': currency}
        path = '/v1/accounts/%s/payments/%s/%s' % (UUID(connection), UUID(payment_id), _PAYMENT_ACTIONS[operation])
        try:
            return self.client.request('POST', path, body)
        except HTTPError as exc:
            if exc.code == 409:
                raise ValueError('Payment control rejected: amount/currency mismatch or terminal state.') from exc
            raise

    def account_state(self, connection):
        return self.client.request('GET', self._account(connection) + '/state')


def _lookup_entry(service, outcome, postal_code):
    key = 'lookup_control.' + str(service)
    if key in UNSUPPORTED:
        raise Unsupported(UNSUPPORTED[key])
    if key not in CAPABILITIES:
        raise Unsupported('Unknown lookup control service.')
    entry = {'outcome': outcome}
    # postal_code is only valid for postal_mismatch; success restores use fixtures.
    if postal_code is not None and outcome == 'postal_mismatch':
        entry['postal_code'] = postal_code
    return entry
