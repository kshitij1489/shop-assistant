"""Map contextual ordering text to a catalog proposal; validation owns execution."""
from .catalog import load_catalog
from .order_interpreter import interpret_order


def parse_order_text(api_key, text, *, basket=None, pending=None, question='', action=None, original_text=None):
    catalog = load_catalog(api_key)
    return {'proposal': interpret_order(text, catalog, basket or [], pending or {}, question,
                                       action, original_text=original_text)}
