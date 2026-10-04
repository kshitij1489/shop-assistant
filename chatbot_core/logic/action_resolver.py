"""Resolve model proposals against authoritative state. No I/O or mutations.

Store adapters supply scoped entities and execute the result through their own
business validators. Model confidence and proposed row numbers confer no authority.
"""
from dataclasses import dataclass
import re

from chatbot_core.llm.schemas import ActionProposal, OrderModifier
from chatbot_core.logic.catalog_match import competing_catalog_items

__all__ = ['NeedsClarification', 'TerminalRejection', 'ResolvedAction', 'competing_catalog_items',
           'ensure_unique_catalog_item', 'resolve_reference', 'validate_basket_targets', 'resolve_action']


class NeedsClarification(ValueError):
    pass


class TerminalRejection(ValueError):
    """The operation cannot be resumed by supplying a missing choice."""


@dataclass(frozen=True)
class ResolvedAction:
    proposal: ActionProposal
    target_id: str | None = None
    basket_targets: tuple[int | None, ...] = ()
    quote_fingerprint: str | None = None


MAX_LISTED_CATALOG_CHOICES = 8


def _words(value):
    return re.findall(r"\w+", str(value).casefold())


def ensure_unique_catalog_item(line, text, catalog):
    """A proposed catalog product must be the only one the customer's words fit."""
    rivals = competing_catalog_items(text, line.item_id, catalog)
    if not rivals:
        return
    chosen = next(row for row in catalog if str(row['id']) == str(line.item_id))
    names = [chosen['name'], *(row['name'] for row in rivals)]
    if len(names) > MAX_LISTED_CATALOG_CHOICES:
        raise NeedsClarification('Several menu items match that name. Please give the full item name.')
    raise NeedsClarification(f"Which item do you mean: {', '.join(names[:-1])} or {names[-1]}?")


def resolve_reference(reference, entities, *, focus=None, missing_is_terminal=False):
    """Entities expose stable IDs and names; partial names must match uniquely."""
    if reference is not None and ((reference.by == 'focus' and reference.value is not None)
            or (reference.by != 'focus' and not (reference.value or '').strip())):
        raise NeedsClarification('Please specify the entry you mean.')
    if reference is None:
        matches = []
    elif reference.by == 'focus':
        matches = [row for row in entities if str(row['id']) == str(focus)] if focus is not None else entities
    elif reference.by == 'id':
        matches = [row for row in entities if str(row['id']) == reference.value]
    else:
        words = _words(reference.value or '')
        matches = [row for row in entities if words and any(
            all(word in _words(name) for word in words)
            for name in [row['name'], *row.get('aliases', [])])]
    if len(matches) == 1:
        return matches[0]['id']
    if not matches and (not entities or missing_is_terminal and reference is not None):
        raise TerminalRejection('No matching entries are available. There is no entry to change or remove.')
    choices = '; '.join(f"#{row['id']} {row['name']}" for row in (matches or entities))
    raise NeedsClarification('Which entry do you mean?' + (f' {choices}.' if choices else ' No matching entries are available.'))


def validate_basket_targets(proposal, basket, *, focus=None):
    """Reject impossible work even when another field still needs clarification."""
    entities = [{'id': row['item_number'], 'name': ' '.join([
        row['name'], row.get('size', ''),
        *[m['name'] for m in row.get('modifiers', [])]])} for row in basket]
    for line in proposal.lines:
        if line.action == 'add' and line.reference is None:
            continue
        if not entities:
            raise TerminalRejection('Your basket is empty. There is no entry to change or remove.')
        try:
            resolve_reference(line.reference, entities, focus=focus, missing_is_terminal=True)
        except NeedsClarification:
            # Ambiguity remains valid unfinished work.
            pass


def resolve_action(proposal, *, basket, addresses=(), focus=None, checkout=None, placed=False,
                   catalog=(), text=''):
    """Rebind each action immediately before execution, including within a turn.

    ``catalog`` rows (id, name, aliases) and the customer's ``text`` let a proposed
    product be checked against similarly named products before it is added.
    """
    action = ActionProposal.model_validate(proposal).model_copy(deep=True)
    kind = action.kind
    # Reject mixed commands rather than silently discarding supplied parameters.
    allowed = {
        'CHANGE_BASKET': {'basket'}, 'SELECT_ADDRESS': {'reference'},
        'SET_FULFILLMENT': {'value'}, 'SET_PAYMENT_METHOD': {'value'},
        'SET_CHECKOUT_FIELD': {'field', 'value'},
        'CLEAR_CHECKOUT_FIELD': {'field'},
    }.get(kind, set())
    supplied = {key for key in ('basket', 'reference', 'field', 'value') if getattr(action, key) is not None}
    if supplied != allowed:
        raise NeedsClarification('Please specify the action and its required details.')
    if placed and kind in {'CHANGE_BASKET', 'SELECT_ADDRESS', 'SET_FULFILLMENT',
                           'SET_PAYMENT_METHOD', 'SET_CHECKOUT_FIELD', 'CLEAR_CHECKOUT_FIELD'}:
        raise TerminalRejection('This order has already been placed. Please contact the store to change it.')
    if kind == 'CHANGE_BASKET':
        validate_basket_targets(action.basket, basket, focus=focus)
        if action.basket.unresolved or action.basket.catalog_miss:
            raise NeedsClarification(next(iter(action.basket.unresolved), 'Please specify the catalog item you mean.'))
        if not action.basket.lines:
            raise NeedsClarification('Which items would you like to change?')
        entities = [{'id': row['item_number'], 'name': ' '.join([
            row['name'], row.get('size', ''),
            *[m['name'] for m in row.get('modifiers', [])]])} for row in basket]
        targets = []
        for line in action.basket.lines:
            if isinstance(line.quantity, float):
                raise NeedsClarification('Please give a whole-number quantity of menu units; fractional quantities cannot be ordered.')
            if line.unresolved:
                raise NeedsClarification(line.unresolved[0])
            # An add that copies a basket row has no catalog choice of its own.
            picks_product = line.action == 'replace' or (line.action == 'add' and line.reference is None)
            if picks_product and line.item_id is not None:
                ensure_unique_catalog_item(line, text, catalog)
            number = None
            if line.action != 'add' or line.reference is not None:
                number = resolve_reference(line.reference, entities, focus=focus, missing_is_terminal=True)
                source = next(row for row in basket if row['item_number'] == number)
                if line.action != 'replace':
                    if line.item_id is not None and line.item_id != source['item_id']:
                        raise NeedsClarification('The selected item does not match the referenced basket entry.')
                    line.item_id = source['item_id']
                if line.action == 'add':
                    line.variant_id = line.variant_id or source['item_variant_id']
                    if line.modifiers is None:
                        line.modifiers = [OrderModifier(**{key: row[key] for key in ('group_id', 'option_id', 'quantity')})
                                          for row in source.get('modifiers', [])]
            # target_number from the model is deliberately overwritten.
            line.target_number = number if line.action != 'add' else None
            targets.append(number)
        return ResolvedAction(action, basket_targets=tuple(targets))
    if kind == 'SELECT_ADDRESS':
        target = resolve_reference(action.reference, addresses)
        return ResolvedAction(action, target_id=str(target))
    if kind == 'CONFIRM_ORDER' and not placed:
        fingerprint = ((checkout or {}).get('quote') or {}).get('fingerprint')
        if not fingerprint or (checkout or {}).get('awaiting'):
            raise NeedsClarification('Please complete checkout and review the total before confirming the order.')
        return ResolvedAction(action, quote_fingerprint=fingerprint)
    return ResolvedAction(action)
