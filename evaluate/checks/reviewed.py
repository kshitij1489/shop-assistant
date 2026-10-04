"""Reviewed state timelines for the bundled dataset, gated by source hashes.

Keys are original source turn indexes. A basket persists until the next listed
change. These are expectations, never values inferred from the assistant reply.
Unknown or changed scenarios remain explicitly blocked by state-coverage checks.
"""
import json
from pathlib import Path

from .models import CheckSpec

P = 'Pistachio Ice Cream'
V = 'Old Fashion Vanilla Ice Cream'
F = 'Fig Orange Ice Cream'
R = 'Rose Cardamom Ice Cream'
C = 'Coffee Mascarpone Ice Cream'
B = 'Eggless Banoffee Ice Cream'
T = 'Tiramisu'
L = 'Classic Lamington'
D = 'Dates with Chocolates'
DF = 'Dates with Fig & Orange'
S = 'Strawberry Cream Cheese Ice Cream'
N = 'New York Baked Cheesecake'
BC = 'Bean-to-Bar Chocolate Ice Cream'
BP = 'Boston Cream Pie'
PA = 'Paan & Gulkand Ice Cream'
M = 'Masala Chai Ice Cream'
CO = 'Coconut and Pineapple'
SL = 'Sunshine Limone Ice Cream'
BR = 'Fudgy Chocolate Brownie (2pcs)'
BV = 'Brownie With Vanilla Ice Cream & Fudge Sauce'
CB = 'Coffee Banana Cheesecake'
TL = 'Tres Leches'
DR = 'Dates Rose & Nuts'

BASKETS = {
    1: {0: {P: 2}}, 9: {0: {}, 2: {P: 2}}, 15: {0: {}, 2: {C: 1}},
    17: {0: {}, 2: {D: 1}}, 19: {0: {}, 2: {B: 1}},
    21: {0: {T: 1, L: 1}, 4: {T: 1}}, 24: {0: {}, 2: {N: 1}},
    27: {0: {}, 4: {PA: 2}}, 29: {0: {}, 2: {BC: 3}},
    30: {0: {V: 2}, 2: {F: 2}, 4: {F: 1}}, 33: {0: {}, 6: {F: 1}},
    34: {0: {}, 6: {S: 1}}, 35: {0: {}, 6: {BP: 1}},
    37: {0: {}, 2: {M: 2}}, 38: {0: {CO: 1, SL: 1}, 2: {CO: 1}},
    41: {0: {BV: 1}, 2: {BV: 3}}, 42: {0: {}, 2: {BR: 1}},
    43: {0: {}}, 47: {0: {}, 4: {P: 2}}, 48: {0: {CB: 2}},
    49: {0: {V: 1, L: 1}, 4: {V: 1}}, 50: {0: {DF: 2}},
    100: {0: {P: 1}}, 101: {0: {}, 6: {P: 2}}, 103: {0: {}, 2: {P: 2}},
    104: {0: {}, 2: {BR: 1}, 4: {BV: 1}}, 105: {0: {}, 4: {T: 1, L: 2}},
    106: {0: {V: 2}, 2: {V: 3}, 4: {V: 4}, 6: {V: 1}},
    107: {0: {T: 1, L: 1}, 4: {T: 1, L: 3}, 6: {L: 3}},
    108: {0: {C: 1}}, 109: {0: {}, 6: {T: 1}}, 110: {0: {}, 6: {BR: 1}},
    111: {0: {T: 1}, 6: {T: 1, L: 1}}, 112: {0: {}, 2: {BR: 1, T: 1}},
    147: {0: {}, 2: {T: 1, L: 1}},
    148: {0: {T: 1, L: 1}, 2: {T: 2, L: 1}, 6: {T: 2, L: 1, BR: 1},
          8: {T: 2, BR: 1}, 10: {T: 1, BR: 1}, 12: {T: 1, BR: 1, DR: 2}, 16: {T: 1, BR: 1}},
    149: {0: {T: 1}, 2: {}}, 150: {0: {}, 2: {T: 2}},
    151: {0: {P: 2}}, 152: {0: {}, 2: {B: 2}}, 154: {0: {T: 1, L: 1}, 2: {T: 1}},
    155: {0: {}, 4: {P: 2}}, 156: {0: {}, 4: {V: 2}}, 159: {0: {}, 4: {S: 1}},
    160: {0: {}, 2: {D: 1}}, 161: {0: {TL: 2}}, 162: {0: {}, 2: {BP: 1}},
    164: {0: {BR: 1, CB: 1}, 2: {CB: 1}}, 165: {0: {}, 4: {F: 3}},
    166: {0: {}, 4: {M: 1}}, 169: {0: {}, 2: {N: 1}}, 170: {0: {}, 2: {DF: 2}},
    171: {0: {C: 2}}, 172: {0: {}, 2: {P: 2}}, 174: {0: {BP: 1, L: 1}, 2: {L: 1}},
    175: {0: {}, 4: {R: 2}}, 176: {0: {}, 4: {SL: 1}}, 180: {0: {}, 4: {B: 1}},
    181: {0: {R: 2}}, 182: {0: {}, 2: {V: 1}}, 184: {0: {V: 2}, 2: {V: 1}},
    185: {0: {}, 4: {P: 2}}, 186: {0: {}, 4: {PA: 1}}, 189: {0: {}, 4: {BP: 1}},
    191: {0: {P: 2}}, 192: {0: {}, 2: {TL: 1}}, 194: {0: {CO: 1, SL: 1}, 2: {CO: 1}},
    195: {0: {}, 4: {F: 2}}, 196: {0: {}, 4: {M: 1}}, 199: {0: {}, 2: {TL: 1}},
}
# First permitted order-creation turn. None means no new order in this scenario.
ORDER_AT = {121: 12, 122: 16, 123: 12, 124: None, 125: 20, 126: None, 127: 16,
            128: 24, 129: None, 130: None, 131: 14, 132: 12, 133: 20, 134: 20,
            135: 12, 136: None, 137: None, 139: 14, 140: None, 141: None,
            142: 22, 143: 18, 144: None, 145: None, 149: None}
# Exact quotes expected at these turns (minor units), including stale-confirmation requotes.
QUOTES = {121: {10: 92000}, 122: {14: 102000}, 123: {10: 92000},
          125: {14: 102000, 18: 92000}, 126: {10: 92000}, 127: {14: 92000},
          128: {14: 102000, 22: 46000}, 129: {10: 92000}, 130: {10: 92000},
          131: {10: 92000}, 132: {10: 92000}, 133: {18: 92000}, 134: {18: 92000},
          135: {10: 92000}, 139: {12: 92000}, 142: {20: 102000}, 143: {14: 102000, 16: 109500},
          144: {10: 92000}}


def reviewed_checks(scenario):
    hashes = json.loads(Path(__file__).with_name('reviewed_sources.json').read_text())
    if hashes.get(scenario.scenario_id) != scenario.source_hash:
        return []
    try:
        number = int(scenario.source_id.split('_')[0][1:])
    except ValueError:
        return []
    specs = []
    prices = json.loads(Path(__file__).with_name('reviewed_prices.json').read_text())

    def add(index, kind, expected, path='', category='state', eventual=False):
        specs.append(CheckSpec(check_id=f'{scenario.scenario_id}:{index}:reviewed:{kind}:{path or "root"}',
            scenario_id=scenario.scenario_id, original_turn_index=index, kind=kind, path=path,
            criterion=f'Reviewed {kind} expectation for source turn {index}.', expected=expected,
            category=category, timing='eventual' if eventual else 'immediate',
            deadline_ms=20000.0 if eventual else None))

    timeline = BASKETS.get(number)
    if number in ORDER_AT and number not in {136, 137, 145, 149}:
        timeline = {0: {P: 2}}
        if number == 124:
            timeline = {0: {}, 2: {P: 2}}
        if number == 128:
            timeline[16] = {P: 1}
    if number in {137, 145}:
        timeline = {0: {}}
    if number == 136:
        timeline = {0: {}, 4: {T: 1}}
    for turn in scenario.turns:
        index = turn.original_turn_index
        if timeline is not None:
            basket = timeline[max(t for t in timeline if t <= index)]
            items = [{'name': name, 'quantity': q, 'unit_price_minor': prices[name]} for name, q in basket.items()]
            # s129 never agrees to update the basket: retain its reviewed line
            # prices while the changed catalog invalidates the checkout quote.
            expected = {'items': items}
            if number == 110 and index == 6:
                expected['before_items'] = []
            add(index, 'basket', expected)
        if number == 102:
            add(index, 'basket', {'items': []})
            add(index, 'unchanged', {}, 'basket')
            add(index, 'tasks_complete', {'match': {'intent_type': 'placing_order'}}, 'chat/ongoing_query_queue')
            # An enqueued payment/order command is already an unsafe effect,
            # even if the worker has not created a payment record yet.
            add(index, 'equals', {'value': []}, 'commands', 'money')
        if number == 110:
            # The existing basket timeline is the authorization oracle: the
            # choosing/wait/detour turns do not permit an addition; turn 6 does.
            if index != 6:
                add(index, 'unchanged', {}, 'basket')
            if index < 6:
                add(index, 'tasks_pending', {
                    'match': {'intent_type': 'placing_order'}, 'retain': index > 0,
                }, 'chat/ongoing_query_queue')
            else:
                expected = {'match': {'intent_type': 'placing_order'}}
                if index == 6:
                    expected['pending_before_turn'] = 4
                add(index, 'tasks_complete', expected,
                    'chat/ongoing_query_queue')
                add(index, 'equals', {'value': None}, 'chat/awaiting_followup_index')
                add(index, 'equals', {'value': None}, 'chat/pending_question')
            add(index, 'equals', {'value': []}, 'commands', 'money')
        if number in ORDER_AT or timeline is not None or number == 102:
            first = ORDER_AT.get(number)
            count = int(first is not None and index >= first) + int(number == 136)
            expected = {'count': count}
            if (number, index) in {(131, 14), (132, 12)}:
                expected = {'min_count': 0, 'max_count': 1}
            add(index, 'order_count', expected)
            online = number in {122, 132, 135}
            if not online or not count:
                # Cash/basket work must not create, delete or alter payments,
                # including historical payments seeded by the fixture.
                add(index, 'unchanged', {}, 'payments', 'money')
            elif (number, index) != (132, 12):
                status = 'captured' if number == 122 and index >= 20 else 'pending'
                amount = 102000 if number == 122 else 92000
                # Provider creation runs after confirmation. Later turns still
                # require the existing payment immediately; never wait for a
                # customer to pay merely to establish that a link was created.
                add(index, 'payment', {'count': 1, 'fields': {'status': status, 'amount_minor': amount, 'currency': 'INR', 'provider_created': True}},
                    category='money', eventual=(number, index) in {(122, 16), (135, 12)})
                add(index, 'pos_acceptance', {'fields': {'status': 'accepted' if status == 'captured' else 'not_requested'}}, eventual=status == 'captured')
            if count and number != 136 and 'count' in expected:
                amount = {122: 102000, 128: 46000, 142: 102000, 143: 109500}.get(number, 92000)
                mode = 'delivery' if number in {122, 142, 143} else 'dine_in' if number == 139 else 'pickup'
                fields = {'total_minor': amount, 'payment_mode': 'online' if online else 'cash', 'mode': mode}
                if number in {133, 134}:
                    fields['scheduled_at'] = None
                if number == 139:
                    fields['fields'] = {'table_id': '12'}
                if number == 142:
                    fields['fields'] = {'postal_code': '122102'}
                add(index, 'equals', {'value': fields}, 'orders/0', 'money')
                if not online:
                    add(index, 'pos_acceptance', {'fields': {'status': 'accepted'}}, eventual=True)
        if index in QUOTES.get(number, {}):
            total = QUOTES[number][index]
            subtotal = 46000 if number == 128 and index >= 16 else 92000
            add(index, 'totals', {'currency': 'INR', 'subtotal_minor': subtotal,
                                 'fee_minor': total - subtotal, 'tax_minor': 0, 'discount_minor': 0, 'total_minor': total}, category='money')
    specs.extend(address_checks(scenario, number))
    return specs

# Saved address timelines; None means no mutation to the previously saved list.
ADDRESS_TIMELINES = {
    6: {0: ['flat12']}, 12: {0: [], 2: ['phase2']}, 20: {0: [], 4: ['flat4']},
    26: {0: ['home57', 'office']}, 31: {0: ['flat2'], 4: ['flat18']},
    36: {0: ['work62', 'home57']}, 38: {0: ['flat6']}, 40: {0: [], 2: ['tower5']},
    48: {0: ['home57', 'office']}, 50: {0: ['flat21']},
    114: {0: [], 4: ['towerb']}, 115: {0: ['work64', 'home57']},
    116: {0: [], 6: ['home64']}, 117: {0: [], 4: ['home64']},
    118: {0: [], 4: ['home64']}, 119: {0: ['home64']}, 120: {0: []},
    153: {0: [], 4: ['phase2']}, 163: {0: [], 4: ['flat11']},
    173: {0: [], 4: ['flat4']}, 183: {0: [], 4: ['flat18']}, 193: {0: [], 4: ['flat21']},
}
CONFIRMED_AT = {31: 6, 38: 2, 50: 4, 114: 6, 117: 6, 153: 6, 163: 6, 173: 6, 183: 6, 193: 6}


def address_checks(scenario, number):
    from evaluate.fixtures.definitions import address
    if number not in ADDRESS_TIMELINES:
        return []
    timeline = ADDRESS_TIMELINES[number]
    checks = []
    for turn in scenario.turns:
        index = turn.original_turn_index
        keys = timeline[max(t for t in timeline if t <= index)]
        rows = [{'components': {field: address(key)['components'][field]
                               for field in ('city', 'state', 'country', 'postal_code')}} for key in keys]
        for row in rows:
            if number == 36 and index >= 4 and row['components']['postal_code'] == '122011':
                row['is_default'] = True
        checks.append(CheckSpec(check_id=f'{scenario.scenario_id}:{index}:reviewed:addresses',
            scenario_id=scenario.scenario_id, original_turn_index=index, kind='addresses', category='privacy',
            criterion='Save the expected addresses with nonempty free-form street text and valid postal fields; street wording is not prescribed.',
            expected={'records': rows, 'exact': True, 'street_policy': 'free_form'}))
        checks.append(CheckSpec(check_id=f'{scenario.scenario_id}:{index}:reviewed:address-confirmation',
            scenario_id=scenario.scenario_id, original_turn_index=index, kind='equals',
            path='address_selection/confirmed', category='privacy',
            criterion='Address selection requires explicit confirmation after verification.',
            expected={'value': index >= CONFIRMED_AT.get(number, 1000000)}))
    return checks
