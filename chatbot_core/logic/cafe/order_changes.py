"""Validate an entire interpretation on an isolated basket before publishing it."""
from copy import deepcopy
from datetime import datetime
from .basket import Basket
from .catalog import load_catalog, validate_selection
from .ordering_limits import MAX_PROPOSAL_LINES, enforce_change
from chatbot_core.llm.schemas import OrderProposal

CONSTRAINT_DISCLOSURE = (
    "You mentioned: {constraints}. Menu descriptions are not an allergen or "
    "cross-contact guarantee, so please confirm suitability with café staff before relying on it."
)


def constraint_disclosure(declared_constraints, lines):
    """Declared dietary/allergy requirements accompany every change that puts items in the basket.

    The chat cannot certify suitability, so the obligation is to disclose, not to
    block. Removals carry no such risk and stay silent.
    """
    constraints = [str(value).strip().rstrip('.') for value in declared_constraints if str(value).strip()]
    if not constraints or all(line['action'] == 'remove' for line in lines):
        return ''
    return CONSTRAINT_DISCLOSURE.format(constraints='; '.join(dict.fromkeys(constraints)))


def apply_proposal(resolved, basket, api_key, *, declared_constraints=()):
    proposal = OrderProposal.model_validate(resolved.proposal.basket).model_dump()
    if proposal["unresolved"]:
        raise ValueError(proposal["unresolved"][0])
    if proposal["catalog_miss"]:
        raise ValueError("I couldn’t find every requested item. Please clarify the menu item names.")
    if not proposal["lines"] or len(proposal["lines"]) > MAX_PROPOSAL_LINES:
        raise ValueError("Please specify the menu items and changes you would like.")
    if len(resolved.basket_targets) != len(proposal['lines']):
        raise ValueError('Every basket change must have a resolved target.')
    catalog = load_catalog(api_key)
    draft = Basket(deepcopy(basket.items), basket.counter)
    replies = []
    touched = set()
    for index, line in enumerate(proposal["lines"]):
        if line["unresolved"]:
            raise ValueError(line["unresolved"][0])
        action = line["action"]
        if action not in {"add", "update", "remove", "replace"}:
            raise ValueError("Would you like to add, update or remove an item?")
        target = None
        if action != 'add':
            target = draft.find_item_by_number(resolved.basket_targets[index])
            if not target or line['target_number'] != target['item_number']:
                raise ValueError('That basket entry is no longer available.')
            if action != 'replace' and line['item_id'] != target['item_id']:
                raise ValueError('The item does not match the resolved basket entry.')
        if target:
            if target["item_number"] in touched:
                raise ValueError("Please specify a single change for each basket entry.")
            touched.add(target["item_number"])
        if action == "remove":
            if line['quantity'] is not None:
                from .catalog import positive_integer
                decrement = positive_integer(line['quantity'])
                if decrement is None or decrement > target['quantity']:
                    raise ValueError('Please give a positive removal quantity no greater than the basket quantity.')
                if decrement < target['quantity']:
                    target['quantity'] -= decrement
                    replies.append(f"Updated {target['name']} quantity to {target['quantity']}.")
                    continue
            draft.remove_item(target["item_number"])
            replies.append(f"Removed {target['name']} ({target['size']}).")
            continue
        item_id = line["item_id"] if action in {'add', 'replace'} else target["item_id"]
        variant = line["variant_id"] if line["variant_id"] is not None or not target or action == 'replace' else target["item_variant_id"]
        quantity = line["quantity"] if line["quantity"] is not None or not target else target["quantity"]
        modifiers = line["modifiers"] if line["modifiers"] is not None else (target.get("modifiers", []) if target and action != 'replace' else [])
        selection = validate_selection(catalog, item_id, variant, quantity, modifiers)
        if action == "add":
            draft.add_validated(selection, enforce=False)
        else:
            target.update(selection, _last_modified=datetime.utcnow().isoformat())
        description = ", ".join(f"{m['quantity']} × {m['name']}" for m in selection["modifiers"])
        replies.append(f"{'Added' if action == 'add' else 'Updated'} {selection['quantity']} × "
                       f"{selection['name']} ({selection['size']})" + (f" with {description}" if description else "") + ".")
    enforce_change(basket.items, draft.items, api_key=api_key)
    basket.items, basket.counter = draft.items, draft.counter
    disclosure = constraint_disclosure(declared_constraints, proposal["lines"])
    return " ".join([*replies, disclosure] if disclosure else replies)
