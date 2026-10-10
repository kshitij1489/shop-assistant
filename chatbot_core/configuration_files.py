"""JSON configuration parsing and deterministic knowledge exports."""
from copy import deepcopy
from decimal import Decimal
import json
import math
from pathlib import Path
import re

from chatbot_core.capabilities import CAPABILITIES

NAME = re.compile(r'[a-z][a-z0-9_]{0,119}\Z')
DOCUMENT_TYPES = ('knowledge', 'intent_classification', 'response_intents')
CONFIGURATION_FILES = {
    'catalog': 'menu_catalog.json',
    'intent_classification': 'intent_classification.json',
    'response_intents': 'response_instructions.json',
    'checkout': 'checkout_settings.json',
    'commerce_policy': 'ordering_policy.json',
}


def parse_json(source):
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('Duplicate JSON object key.')
            result[key] = value
        return result

    def constant(value):
        raise ValueError('JSON numbers must be finite.')

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            constant(value)
        return result

    if isinstance(source, (str, bytes)):
        return json.loads(source, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)
    def native(value):
        if isinstance(value, Decimal) and value.is_finite():
            return str(value)
        raise ValueError('Configuration must contain JSON-compatible values.')

    # Forms use Decimal for currency fields; files represent them as strings.
    return json.loads(json.dumps(source, allow_nan=False, default=native), object_pairs_hook=pairs)


def document_records(dtype, source, *, allow_legacy_classifications=True, require_type=False):
    data = parse_json(source)
    if dtype not in DOCUMENT_TYPES:
        raise ValueError('Unsupported document type.')
    if isinstance(data, dict) and ('document_type' in data or 'documents' in data):
        if set(data) != {'document_type', 'documents'} or data['document_type'] != dtype:
            raise ValueError('The JSON document_type must match the selected configuration type.')
        data = data['documents']
        allow_legacy_classifications = False
    elif require_type:
        raise ValueError('Document imports require document_type and documents. '
                         'Use a typed export so the selected type can be checked before saving.')
    if not isinstance(data, dict) or not data:
        raise ValueError('Root must map intents to topic objects.')
    records = []
    for intent, topics in data.items():
        capability = CAPABILITIES.get(intent)
        if capability is None or not NAME.fullmatch(intent) or not isinstance(topics, dict) or not topics:
            raise ValueError(f'Invalid intent: {intent}.')
        for topic, payload in topics.items():
            if not NAME.fullmatch(topic):
                raise ValueError('Topic names must use lowercase letters, digits and underscores.')
            if dtype == 'intent_classification':
                if not capability.supports(topic):
                    raise ValueError(f'Unsupported route: {intent}/{topic}.')
                if isinstance(payload, str) and not allow_legacy_classifications:
                    raise ValueError(f'Classification {intent}/{topic} must be an object with a description '
                                     'of the customer request. Import response instructions as Response Intents.')
                payload = {'description': payload} if isinstance(payload, str) else payload
                allowed = {'description', 'examples', 'enabled', 'required_knowledge', 'required_settings', 'catalog_references'}
                if (not isinstance(payload, dict) or payload.keys() - allowed
                        or not isinstance(payload.get('description'), str) or not payload['description'].strip()
                        or type(payload.get('enabled', True)) is not bool):
                    raise ValueError(f'Invalid classification: {intent}/{topic}.')
                for field in allowed - {'description', 'enabled'}:
                    values = payload.get(field, [])
                    if not isinstance(values, list) or (field != 'catalog_references' and
                            any(not isinstance(v, str) or not v.strip() for v in values)):
                        raise ValueError(f'Invalid classification {field}: {intent}/{topic}.')
                payload = {'enabled': True, **payload}
            if dtype == 'response_intents' and (not isinstance(payload, str) or not payload.strip()):
                raise ValueError(f'Response instructions are required: {intent}/{topic}.')
            records.append(dict(dtype=dtype, intent=intent, sub_intent=topic, payload=payload))
    return sorted(records, key=lambda record: (record['intent'], record['sub_intent']))


def catalog_knowledge(catalog):
    """Combine reviewed topic facts with catalog-derived listings and prices.

    Operational quantities and variant labels are not public serving-size facts.
    A catalog with reviewed knowledge must explicitly identify its listed variant.
    """
    if not isinstance(catalog, dict) or not isinstance(catalog.get('menu_items'), list):
        raise ValueError('Catalog must contain a menu_items list.')
    if not isinstance(catalog.get('currency'), str) or not re.fullmatch(r'[A-Z]{3}', catalog['currency']):
        raise ValueError('Catalog must declare its currency.')
    topics = deepcopy(catalog.get('knowledge'))
    if not isinstance(topics, dict):
        raise ValueError('Catalog knowledge must be a topic object.')
    for topic in ('pricing', 'explore_options', 'availability', 'specialty_items'):
        if topic in topics and not isinstance(topics[topic], dict):
            raise ValueError(f'Catalog knowledge {topic} must be an object.')
    prices, variants, categories, groups = {}, {}, {}, {}
    include_variants = 'variants_by_item' in topics.get('pricing', {})
    for row in catalog['menu_items']:
        if not isinstance(row, dict):
            raise ValueError('Catalog items must be objects.')
        name, category = row.get('name'), row.get('menu_category')
        listed, pricing = row.get('listed_variant'), row.get('pricing')
        if (not isinstance(name, str) or not name.strip() or name in prices
                or not isinstance(category, str) or not category.strip()
                or not isinstance(pricing, dict) or not isinstance(listed, str) or listed not in pricing):
            raise ValueError('Each listed item needs a unique name, category and listed variant price.')
        try:
            amount = Decimal(str(pricing[listed]).removeprefix('₹').strip())
        except ArithmeticError as exc:
            raise ValueError('Listed price must be a decimal amount.') from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError('Listed price must be finite and nonnegative.')
        prices[name] = {'listed_price': float(amount) if amount % 1 else int(amount),
                        'currency': catalog['currency'], 'size': row.get('listed_size')}
        if include_variants:
            variants[name] = {}
            for label, value in pricing.items():
                try:
                    price = Decimal(str(value).removeprefix('₹').strip())
                except ArithmeticError as exc:
                    raise ValueError('Variant prices must be decimal amounts.') from exc
                if not price.is_finite() or price < 0:
                    raise ValueError('Variant prices must be finite and nonnegative.')
                variants[name][label] = format(price.normalize(), 'f')
        categories[name] = category
        groups.setdefault(category, []).append(name)
    topics.setdefault('pricing', {})['items'] = prices
    if include_variants:
        topics['pricing']['variants_by_item'] = variants
    topics['menu_category'] = categories
    topics.setdefault('explore_options', {})['categories'] = groups
    topics.setdefault('availability', {})['listed_items'] = list(categories)
    specialty = topics.get('specialty_items', {})
    if 'category' in specialty:
        specialty['items'] = groups.get(specialty['category'], [])
    document_records('knowledge', {'menu_items': topics})
    return {'menu_items': topics}


def knowledge_exports(root):
    """Render all derived files from catalog and the two authored knowledge files."""
    root = Path(root).resolve()

    def read(name):
        path = (root / name).resolve()
        if not path.is_relative_to(root):
            raise ValueError('Configuration input escapes its directory.')
        return parse_json(path.read_text(encoding='utf-8'))

    menu = catalog_knowledge(read(CONFIGURATION_FILES['catalog']))
    combined = {}
    for data in (read('01_cafe_knowledge.json'), menu, read('03_ordering_knowledge.json')):
        document_records('knowledge', data)
        for intent, topics in data.items():
            target = combined.setdefault(intent, {})
            if target.keys() & topics.keys():
                raise ValueError('Overlapping knowledge inputs.')
            target.update(topics)
    return {'02_menu_knowledge.json': menu, 'knowledge_base.json': combined,
            'knowledge_records.json': document_records('knowledge', combined)}
