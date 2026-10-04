"""Closed, reviewed fixtures referenced by hash in the shared ScenarioPlan."""
from typing import Literal
from pydantic import Field
from evaluate.contracts.models import StrictModel
from evaluate.identity import canonical_hash


class Fixture(StrictModel):
    kind: Literal['catalog_assertion', 'branch_policy', 'addresses', 'basket',
                  'terminal_order', 'foreign_draft', 'foreign_address', 'foreign_order',
                  'foreign_pending', 'settings_assertion']
    addresses: list[str] = Field(default_factory=list)
    items: dict[str, int] = Field(default_factory=dict)
    branch: Literal['quantity', 'variation'] | None = None
    setting: Literal['scheduling', 'dine_in', 'no_dine_in'] | None = None


# Synthetic, explicitly supplied dataset addresses. No coordinates are needed.
ADDRESSES = {
    'home57': ('Home', 'Flat 9', '', '', 'Sector 57', '122011'),
    'office': ('Office', '14', '', 'DLF Cyber City', '', '122002'),
    'work62': ('Work', 'Flat 1', '', '', 'Sector 62', '122101'),
    'home64': ('Home', 'Flat 8', '', 'Ramgarh Road', 'Sector 64', '122102'),
    'work64': ('Work', 'Flat 8', '', 'Ramgarh Road', 'Sector 64', '122102'),
    'flat12': ('Home', 'Flat 12', 'Tower B', '', 'Sector 56', '122011'),
    'phase2': ('Home', '22', '', '', 'DLF Phase 2', '122002'),
    'flat4': ('Home', 'Flat 4', '', 'Lane 3', 'Sector 43', '122003'),
    'flat2': ('Home', 'Flat 2', '', '', 'Sector 45', '122003'),
    'flat18': ('Home', 'Flat 18', '', '', 'Sector 45', '122003'),
    'flat6': ('Home', 'Flat 6', '', '', 'Sector 55', '122003'),
    'tower5': ('Home', 'Flat 8', 'Tower 5', '', 'Sector 49', '122018'),
    'flat21': ('Home', 'Flat 21', '', '', 'Sector 51', '122018'),
    'towerb': ('Home', 'Flat 8', 'Tower B', 'Ramgarh Road', 'Sector 64', '122102'),
    'flat11': ('Home', 'Flat 11', '', '', 'Sector 56', '122011'),
}
ADDRESS_CASES = {
    's06_save_address': ['flat12'], 's12_address_missing_pincode': ['phase2'],
    's20_address_then_wait': ['flat4'], 's26_two_addresses_then_choose': ['home57', 'office'],
    's31_wrong_address_then_fix': ['flat2', 'flat18'],
    's36_addresses_default_and_map_pin': ['work62', 'home57'],
    's38_confirm_address_and_remove': ['flat6'], 's40_pincode_and_eggless': ['tower5'],
    's48_payment_detour_then_address': ['home57', 'office'],
    's50_cart_detour_then_confirm_address': ['flat21'],
    's114_address_incremental_correction': ['towerb'],
    's115_address_ambiguous_selection': ['work64', 'home57'],
    's116_address_invalid_pin': ['home64'], 's117_coverage_failure_retry': ['home64'],
    's118_geocode_mismatch': ['home64'], 's119_foreign_address_id': ['home64'],
    's120_forged_saved_address_claim': [], 's153_hi_address_ruk_jao': ['phase2'],
    's163_es_direccion_espera': ['flat11'], 's173_fr_adresse_attends': ['flat4'],
    's183_ru_adres_podozhdi': ['flat18'], 's193_pt_endereco_pera': ['flat21'],
}


def address(key):
    label, flat, building, street, sector, pin = ADDRESSES[key]
    components = dict(street_address=', '.join(v for v in (flat, building, street, sector) if v),
                      city='Gurugram', state='Haryana', postal_code=pin, country='India')
    components = {k: v for k, v in components.items() if v}
    return dict(label=label, components=components,
                address_line=', '.join(components.values()),
                location_coordinates=None)


FIXTURES = {
    'catalog-contract': Fixture(kind='catalog_assertion'),
    'quantity-branch': Fixture(kind='branch_policy', branch='quantity'),
    'branch-variation': Fixture(kind='branch_policy', branch='variation'),
    'home-office': Fixture(kind='addresses', addresses=['home57', 'office']),
    'work-home': Fixture(kind='addresses', addresses=['work62', 'home57']),
    'home-and-work': Fixture(kind='addresses', addresses=['work64', 'home57']),
    'vanilla-lamington': Fixture(kind='basket', items={'Old Fashion Vanilla Ice Cream': 1, 'Classic Lamington': 1}),
    'foreign-address': Fixture(kind='foreign_address', addresses=['home64']),
    'terminal-order': Fixture(kind='terminal_order', items={'Pistachio Ice Cream': 2}),
    'foreign-draft': Fixture(kind='foreign_draft', items={'Pistachio Ice Cream': 2}),
    'foreign-order': Fixture(kind='foreign_order'),
    'foreign-pending': Fixture(kind='foreign_pending'),
    'schedule-policy': Fixture(kind='settings_assertion', setting='scheduling'),
    'dine-in-policy': Fixture(kind='settings_assertion', setting='dine_in'),
    'no-dine-in-policy': Fixture(kind='settings_assertion', setting='no_dine_in'),
}


def fixture_hashes():
    result = {key: canonical_hash({'fixture': value.model_dump(),
                                  'addresses': {k: address(k) for k in value.addresses}})
              for key, value in FIXTURES.items()}
    result['address-stubs-v1'] = canonical_hash({'addresses': ADDRESSES, 'cases': ADDRESS_CASES})
    return result


def get_fixture(key, digest):
    fixture = FIXTURES.get(key)
    if fixture is None or fixture_hashes()[key] != digest:
        raise ValueError('Unknown or stale reviewed fixture')
    return fixture
