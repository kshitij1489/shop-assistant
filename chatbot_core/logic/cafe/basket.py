from typing import List, Dict, Optional, Any
import logging
from decimal import Decimal, InvalidOperation
from datetime import datetime
from copy import deepcopy
from commerce.policy import MAX_ITEM_QUANTITY
from commerce.pricing import minor
from .catalog import modifier_key, positive_integer
from .ordering_limits import OrderingRejected, enforce_change
from chatbot_core.knowledge_cache import get_item_pricing_cache

logger = logging.getLogger(__name__)

def search_cache(api_key: str, name: str, size: str):
    """
    Look up one item's variant and price.
    Returns: {"item_id": "...", "item_variant_id": "...", "unit_price": "..."} or None if not found.
    """
    try:
        item = get_item_pricing_cache().get(api_key, {}).get(name, None)
    except KeyError:
        return None

    if not isinstance(item, dict):
        return None

    variant_id = item.get("item_variant_map", {}).get(size)
    if not variant_id:
        return None

    unit_price = item.get("pricing", {}).get(variant_id)
    if unit_price is None:
        return None

    price = _price(unit_price)
    if price is None or not item.get("item_id"):
        return None
    return {
        "item_id": item["item_id"],
        "item_variant_id": variant_id,
        "unit_price": str(price)
    }

def _price(value):
    try:
        price = Decimal(str(value))
        return price if price.is_finite() and price >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


class Basket:
    def __init__(self, items: Optional[List[Dict[str, Any]]] = None, counter: int = 1) -> None:
        self.items: List[Dict[str, Any]] = items or []
        self.counter: int = counter
        self.rejection: str | None = None

    def add_item(self, name: str, api_key: str, size: str, quantity: int | str | None = 1, modifiers=None) -> bool:
        self.rejection = None
        qty = positive_integer(quantity)
        if qty is None or qty < 1:
            logger.warning("Ignoring add_item with a malformed quantity")
            return False
        if qty > MAX_ITEM_QUANTITY:
            self.rejection = "That quantity is above the supported maximum."
            return False

        item_details = search_cache(api_key, name, size)
        if not item_details:
            logger.info("Item is unavailable for add_item", extra={"item_name": name})
            return False

        if modifiers:
            from .catalog import load_catalog, validate_selection
            try:
                item_details = validate_selection(load_catalog(api_key), item_details["item_id"],
                                                  item_details["item_variant_id"], qty, modifiers)
            except ValueError as exc:
                self.rejection = str(exc)
                return False
        proposed, counter = self._with_addition(name, size, qty, item_details, modifiers)
        if not self._accept(proposed, counter, api_key):
            return False
        logger.info("Added item to basket", extra={"item_name": name, "quantity": qty})
        return True

    def _with_addition(self, name, size, qty, item_details, modifiers):
        proposed = deepcopy(self.items)
        counter = self.counter
        if name is not None and size is not None:
            for item in proposed:
                same_name = (item.get("name") or "").lower() == (name or "").lower()
                same_selection = same_name and item.get("size") == size and modifier_key(item.get("modifiers")) == modifier_key(modifiers)
                if not same_selection:
                    continue
                current_qty = positive_integer(item.get("quantity")) or 0
                item["quantity"] = current_qty + qty
                item["unit_price"] = item_details["unit_price"]
                if modifiers:
                    item["modifiers"] = deepcopy(item_details["modifiers"])
                item["_last_modified"] = datetime.utcnow().isoformat()
                return proposed, counter
        item = {"name": name, "size": size, "quantity": qty, "item_number": counter,
                "_last_modified": datetime.utcnow().isoformat(), "item_id": item_details["item_id"],
                "item_variant_id": item_details["item_variant_id"], "unit_price": item_details["unit_price"]}
        if modifiers:
            item["modifiers"] = item_details["modifiers"]
        proposed.append(item)
        return proposed, counter + 1

    def _accept(self, proposed, counter, api_key) -> bool:
        try:
            enforce_change(self.items, proposed, api_key=api_key)
        except OrderingRejected as exc:
            self.rejection = str(exc)
            return False
        self.items = proposed
        self.counter = counter
        return True

    def add_validated(self, selection, *, api_key=None, enforce=True):
        """Only callers that validated against a fresh tenant catalog may use this."""
        self.rejection = None
        proposed = deepcopy(self.items)
        counter = self.counter
        merged = False
        for item in proposed:
            if (item.get("item_id"), item.get("item_variant_id"), modifier_key(item.get("modifiers"))) == (
                    selection["item_id"], selection["item_variant_id"], modifier_key(selection.get("modifiers"))):
                item["quantity"] += selection["quantity"]
                item["unit_price"] = selection["unit_price"]
                item["modifiers"] = deepcopy(selection.get("modifiers", []))
                item["_last_modified"] = datetime.utcnow().isoformat()
                merged = True
                break
        if not merged:
            proposed.append({**deepcopy(selection), "item_number": counter,
                             "_last_modified": datetime.utcnow().isoformat()})
            counter += 1
        if not enforce:
            self.items = proposed
            self.counter = counter
            return
        if not self._accept(proposed, counter, api_key):
            raise OrderingRejected(self.rejection or "That basket change is above the ordering limits.")

    def update_item(self, item_number: int, api_key: str, size: Optional[str] = None, quantity: Optional[Any] = None) -> bool:
        self.rejection = None
        item = self.find_item_by_number(item_number)
        if not item:
            logger.warning("Basket item number was not found", extra={"item_number": item_number})
            return False

        if quantity is not None:
            quantity = positive_integer(quantity)
            if quantity is None:
                self.rejection = "How many would you like? Please give a positive whole number."
                return False
        from .catalog import load_catalog, validate_selection
        try:
            catalog = load_catalog(api_key)
            variant_id = item["item_variant_id"]
            if size is not None:
                variant_id = next((v["id"] for v in catalog[item["item_id"]]["variants"] if v["name"] == size), None)
            updated = validate_selection(catalog, item["item_id"], variant_id,
                                         quantity if quantity is not None else item["quantity"], item.get("modifiers", []))
        except (ValueError, KeyError) as exc:
            self.rejection = str(exc)
            return False
        proposed = deepcopy(self.items)
        target = next(row for row in proposed if row.get("item_number") == item_number)
        target.update(updated, _last_modified=datetime.utcnow().isoformat())
        if not self._accept(proposed, self.counter, api_key):
            return False
        logger.info("Updated basket item", extra={"item_number": item_number})
        return True

    def remove_item(self, item_number: int) -> bool:
        item = self.find_item_by_number(item_number)
        if not item:
            logger.warning(f"Item number '{item_number}' not found in basket.")
            return False

        self.items.remove(item)
        logger.info(f"Removed item from basket: {item}")
        return True

    def get_item(self, name: str) -> Optional[Dict[str, Any]]:
        return Basket.find_item(self, name)[0]

    def find_item_by_number(self, item_number: int) -> Optional[Dict[str, Any]]:
        for item in self.items:
            if item.get("item_number") == item_number:
                return item
        return None

    def clear(self) -> None:
        logger.info("Cleared all items from basket.")
        self.items = []

    def is_empty(self) -> bool:
        return not self.items

    def summary(self, *, currency: str | None = None, exponent: int | None = None) -> List[Dict[str, Any]]:
        """Public basket lines in minor units. An empty basket needs no currency."""
        if not self.items:
            return []
        if not isinstance(currency, str) or isinstance(exponent, bool) or not isinstance(exponent, int):
            raise ValueError("Basket amounts need a currency and exponent.")
        details: List[Dict[str, Any]] = []
        for it in self.items:
            qty = positive_integer(it.get("quantity"))
            if qty is None:
                raise ValueError("A basket quantity is invalid.")
            unit = minor(it.get("unit_price"), exponent)
            modifier_unit = 0
            public_modifiers = []
            for choice in it.get("modifiers") or []:
                count = positive_integer(choice.get("quantity"))
                if count is None:
                    raise ValueError("A modifier quantity is invalid.")
                choice_unit = minor(choice.get("unit_price"), exponent)
                modifier_unit += choice_unit * count
                public_modifiers.append({
                    "name": choice.get("name"), "quantity": count,
                    "group_id": choice.get("group_id"), "option_id": choice.get("option_id"),
                    "unit_price_minor": choice_unit,
                })
            per_unit = unit + modifier_unit
            row = {"name": it.get("name"), "size": it.get("size"), "quantity": qty,
                   "currency": currency, "exponent": exponent,
                   "unit_price_minor": per_unit, "line_total_minor": per_unit * qty}
            if public_modifiers:
                row["modifiers"] = public_modifiers
            details.append(row)
        return details

    def most_recent(self) -> Optional[Dict[str, Any]]:
        if not self.items:
            return None
        return max(self.items, key=lambda item: item.get("_last_modified", ""))

    def to_dict(self) -> Dict[str, Any]:
        return {"items": self.items, "counter": self.counter}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Basket':
        items = []
        for d in data.get("items", []):
            if isinstance(d, dict):
                d["quantity"] = positive_integer(d.get("quantity"))
                items.append(d)
            else:
                logger.warning(f"Invalid item data in basket: {d}")
        counter = data.get("counter", len(items) + 1)
        return cls(items=items, counter=counter)
    @staticmethod
    def _is_complete(item: Dict[str, Any]) -> bool:
        return all([
            item.get("name") is not None,
            item.get("size") is not None,
            item.get("quantity") is not None
        ])

    @staticmethod
    def missing_fields(item: Dict[str, Any]) -> List[str]:
        missing = []
        if not item.get("name"):
            missing.append("name")
        if not item.get("size"):
            missing.append("size")
        if not item.get("quantity") or item.get("quantity", 0) < 1:
            missing.append("quantity")
        return missing

    @staticmethod
    def find_item(
        basket: "Basket",
        name: Optional[str] = None,
        size: Optional[str] = None,
        quantity: Optional[Any] = None
    ) -> List[Dict[str, Any]]:
        if not name:
            return []

        results = []
        for item in basket.items:
            if item.get("name", "").lower() != name.lower():
                continue
            if size is not None and item.get("size") != size:
                continue
            if quantity is not None and positive_integer(item.get("quantity")) != positive_integer(quantity):
                continue
            results.append(item)

        return results
