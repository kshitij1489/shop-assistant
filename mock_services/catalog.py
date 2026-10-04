"""Convert knowledge prices into an explicitly synthetic transactional catalog."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path


DEFAULT_SEED = Path(__file__).resolve().parents[1] / 'test_data/02_menu_knowledge.json'


def identity(kind, name):
    return 'mock-' + kind + '-' + hashlib.sha256(name.encode()).hexdigest()[:20]


def load_catalog(path=DEFAULT_SEED):
    menu = json.loads(Path(path).read_text())['menu_items']
    categories, items = {}, []
    for name, price in menu['pricing']['items'].items():
        if price['currency'] != 'INR':
            raise ValueError('The mock catalog expects INR prices.')
        amount = Decimal(str(price['listed_price']))
        if not amount.is_finite() or amount < 0 or amount != amount.quantize(Decimal('.01')):
            raise ValueError('Invalid fixture price.')
        category = menu['menu_category'][name]
        categories[category] = dict(external_id=identity('category', category), name=category, available=True)
        items.append(dict(external_id=identity('item', name), name=name,
            description='Synthetic checkout fixture; serving size and live availability are unverified.',
            available=True, category_id=categories[category]['external_id'],
            variants=[dict(external_id='mock-standard', name='Standard (test)',
                           price=format(amount, '.2f'), available=True)], modifier_groups=[]))
    return dict(currency='INR', categories=list(categories.values()), modifier_groups=[], items=items)
