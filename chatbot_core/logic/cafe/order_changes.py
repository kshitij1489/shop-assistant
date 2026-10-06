"""Validate an entire interpretation on an isolated basket before publishing it."""
from copy import deepcopy
from datetime import datetime
from .basket import Basket
from .catalog import load_catalog, selection_unit_total, validate_selection
from .ordering_limits import MAX_PROPOSAL_LINES, enforce_change, format_minor, load_policy
from commerce.pricing import minor
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
    if len(proposal["lines"]) > MAX_PROPOSAL_LINES:
        raise ValueError("Please specify the menu items and changes you would like.")
    if len(resolved.basket_targets) != len(proposal['lines']):
        raise ValueError('Every basket change must have a resolved target.')
    if not proposal['lines']:
        if not proposal['preserved_references'] or not resolved.preserved_basket_targets:
            raise ValueError("Please specify the menu items and changes you would like.")
        if any(basket.find_item_by_number(number) is None
               for number in resolved.preserved_basket_targets):
            raise ValueError('That basket entry is no longer available.')
        return 'Your basket is unchanged.'
    catalog = load_catalog(api_key)
    policy = load_policy(api_key=api_key)
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
        # The amount is established by selection validation, including modifiers;
        # the response composer must retain it rather than infer a price from prose.
        price = (f" at {policy.currency} "
                 f"{format_minor(minor(selection_unit_total(selection), policy.exponent), policy.exponent)} each"
                 if policy is not None else '')
        replies.append(f"{'Added' if action == 'add' else 'Updated'} {selection['quantity']} × "
                       f"{selection['name']} ({selection['size']})" + (f" with {description}" if description else "")
                       + price + ".")
    # Also catch indirect edits, such as an addition merging into a retained row.
    if any(draft.find_item_by_number(number) != basket.find_item_by_number(number)
           for number in resolved.preserved_basket_targets):
        raise ValueError('An item requested unchanged would be modified. Please clarify which items to change.')
    enforce_change(basket.items, draft.items, api_key=api_key)
    basket.items, basket.counter = draft.items, draft.counter
    disclosure = constraint_disclosure(declared_constraints, proposal["lines"])
    return " ".join([*replies, disclosure] if disclosure else replies)
