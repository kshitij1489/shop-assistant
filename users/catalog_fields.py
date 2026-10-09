"""Shared definitions for catalog metadata parsing and editor rendering."""
import json


# name, label, empty value, editor rows, full row
CATALOG_FIELDS = (
    ('dietary_preferences', 'Dietary Preferences (JSON)', dict, 6, False),
    ('allergens', 'Allergens (JSON)', dict, 6, False),
    ('preparation', 'Preparation (JSON or text)', dict, 4, False),
    ('nutrition', 'Nutrition (JSON)', dict, 4, False),
    ('explore_options', 'Explore Options (JSON)', dict, 4, False),
    ('ingredients', 'Ingredients (JSON array)', list, 4, False),
    ('recommendations', 'Recommendations (JSON array)', list, 4, False),
    ('specialty_items', 'Specialty Items (JSON array)', list, 4, False),
    ('source_quality', 'Source Quality (JSON array)', list, 4, False),
    ('pairings', 'Pairings (JSON array)', list, 4, True),
    ('flavor_profile', 'Flavor Profile (text)', str, 4, True),
)


def catalog_editor_fields(metadata):
    return [dict(name=name, label=label, value=getattr(metadata, name), rows=rows,
                 full_row=full_row, is_json=default is not str, empty=json.dumps(default()),
                 source_id=f'catalog-json-{name}')
            for name, label, default, rows, full_row in CATALOG_FIELDS]


def parse_catalog_fields(data):
    values = {}
    for name, label, default, _, _ in CATALOG_FIELDS:
        raw = data.get(name, '').strip()
        if default is str:
            values[name] = raw or None
        elif not raw:
            values[name] = default()
        else:
            try:
                values[name] = json.loads(raw)
            except json.JSONDecodeError as exc:
                if name != 'preparation':
                    raise ValueError(f"{name.replace('_', ' ').title()} must be valid JSON.") from exc
                values[name] = {'text': raw}
    return values
