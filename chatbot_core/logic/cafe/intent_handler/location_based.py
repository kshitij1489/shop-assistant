"""Location business operations for the graph's pending-intent/handoff contract.

Drafts live in delivery_address; questions and handoffs use BaseIntent's existing
serializable state. The conversation graph owns routing and session I/O.

Draft lifecycle: a row created by this conversation is ``provisional`` until the
customer confirms it. Denying or restarting such a draft keeps the row as
``replace_address_id`` so the next save overwrites it instead of growing the
address book. Established saved addresses are never replaced this way.
"""
from __future__ import annotations

import logging
import re
from uuid import UUID

from chatbot_core.capabilities import CAPABILITIES
from .base import BaseIntent
from chatbot_core.logic.cafe.prompt_builder import extract_address_with_gpt
from chatbot_core.logic.cafe.location_utils import (
    comparable_address, extract_pincode, STREET_FIELDS,
    format_address, get_missing_address_keys, normalize_address, normalize_pincode,
)
from chatbot_core.logic.cafe.db_utils import (
    list_addresses, create_address, update_address_fields,
    verify_delivery_pincode,
)

logger = logging.getLogger(__name__)
_UUID = re.compile(r"(?<!\w)[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?!\w)", re.I)
_MISSING_LABELS = {
    "street_address": "street address", "city": "city", "state": "state",
    "country": "country", "postal_code": "valid 6-digit pincode",
}


def _format_address_line(addr):
    label = f"[{addr.label}] " if addr.label else ""
    default = " (default)" if addr.is_default else ""
    return f"{label}{(addr.address_line or '').strip()}{default}"


def _list_addresses_response(addresses):
    if not addresses:
        return "You don’t have any saved addresses yet. Please share your full delivery address."
    return "Here are your saved addresses:\n" + "\n".join(
        f"• {addr.id} — {_format_address_line(addr)}" for addr in addresses
    )


def update_address(updates, delivery_address):
    """Merge partial extraction without erasing known fields or accepting metadata."""
    updates = normalize_address(updates)
    if "street_address" in updates:
        for key in STREET_FIELDS:
            delivery_address.pop(key, None)
    delivery_address.update(updates)
    for key in ("latitude", "longitude", "_shared_coordinates"):
        delivery_address.pop(key, None)
    # Derived lines from older sessions become stale after a partial update.
    delivery_address.pop("address_line1", None)
    delivery_address.pop("address_line2", None)
    return delivery_address


def discard_draft(delivery_address):
    """Drop the drafted fields; a provisional row stays replaceable by the next save."""
    replaceable = (delivery_address.get("address_id") if delivery_address.get("provisional")
                   else delivery_address.get("replace_address_id"))
    delivery_address.clear()
    if replaceable:
        delivery_address["replace_address_id"] = replaceable


class _AddressUnavailable(Exception):
    """The targeted saved row cannot be written; the message is customer-facing."""


class LocationBasedIntent(BaseIntent):
    COVERAGE_UNAVAILABLE_RESPONSE = (
        "I couldn’t verify delivery coverage right now. Please try again shortly or contact the café."
    )
    SUB_INTENT_NAMES = CAPABILITIES["location_based"].sub_intents

    def __init__(self, *, main_query, sub_intent, tenant, chat_id, query_id=0, response="",
                 is_complete=False, ignored_count=0, basket_item=None,
                 follow_up_question=None, follow_up_reply=None):
        super().__init__(main_query=main_query, sub_intent=sub_intent, tenant=tenant, chat_id=chat_id,
                         query_id=query_id, response=response, is_complete=is_complete,
                         ignored_count=ignored_count, basket_item=basket_item or {},
                         follow_up_question=follow_up_question or [], follow_up_reply=follow_up_reply or [])
        self.intent_type = "location_based"
        self.promp_restriction = False

    def _ask(self, question):
        self.follow_up_question = [question]
        self.is_complete = False
        return question

    def _finish(self, response):
        self.follow_up_question = []
        self.is_complete = True
        return response

    def _confirmation(self, address):
        text = format_address(address)
        self.request_handoff("location_based", sub_intent="confirm_delivery_address",
                             main_query=f"Delivery address: {text}",
                             follow_up_question=[f"Please confirm your delivery address: {text}."])
        return self._finish("")

    def _saved_address(self, customer, address_id):
        try:
            address_id = UUID(str(address_id))
        except (ValueError, TypeError, AttributeError):
            return None
        return next((addr for addr in list_addresses(customer) if addr.id == address_id), None)

    def _select_address(self, query, addresses):
        ids = _UUID.findall(query or "")
        if ids:
            if len(set(value.lower() for value in ids)) != 1:
                return None
            return next((addr for addr in addresses if str(addr.id).lower() == ids[0].lower()), None)
        matches = [addr for addr in addresses if addr.label and addr.label.strip() and re.search(
            rf"(?<!\w){re.escape(addr.label.strip())}(?!\w)", query or "", re.I)]
        if len(matches) == 1:
            return matches[0]
        if not matches and re.search(r"\b(?:use|choose|select) (?:my |the )?default\b", query or "", re.I):
            return next((addr for addr in addresses if addr.is_default), None)
        return None

    def _load_address(self, addr, delivery_address):
        components = normalize_address(addr.components)
        if not components:
            # Older rows may contain only a rendered address.
            extracted = normalize_address(extract_address_with_gpt(addr.address_line))
            components = {**extracted, **components}
        delivery_address.clear()
        delivery_address.update(components, address_id=str(addr.id))

    def choose_delivery_address(self, customer, query, delivery_address, checklist):
        addresses = list_addresses(customer)
        if not addresses:
            self.sub_intent = "add_delivery_address"
        action = self.resolved_action
        addr = (next((row for row in addresses if str(row.id) == action.target_id), None)
                if action and action.proposal.kind == 'SELECT_ADDRESS'
                else self._select_address(query, addresses))
        if addr is None:
            return self._ask(_list_addresses_response(addresses) + (
                "\nWhich address should I use? Please share its label or ID." if addresses else ""))
        self._load_address(addr, delivery_address)
        checklist["location"] = False
        return self._confirmation(delivery_address)

    def _save_address(self, customer, delivery_address, *, label=None, set_as_default=True):
        missing = get_missing_address_keys(delivery_address)
        if missing:
            return self._ask("Please share the following address details: " +
                             ", ".join(_MISSING_LABELS[key] for key in missing) + ".")
        components = normalize_address(delivery_address)
        coverage = verify_delivery_pincode(customer.tenant, components["postal_code"])
        if coverage is False:
            return self._ask(f"We don’t currently deliver to {components['postal_code']}. Please share an address in another area.")
        if coverage is not True:
            return self._ask(self.COVERAGE_UNAVAILABLE_RESPONSE)
        try:
            addr, provisional = self._write_address(customer, delivery_address, components,
                                                    label=label, set_as_default=set_as_default)
        except _AddressUnavailable as exc:
            return self._ask(str(exc))
        delivery_address.clear()
        delivery_address.update(components, address_id=str(addr.id))
        if provisional:
            delivery_address["provisional"] = True
        return self._confirmation(delivery_address)

    def _write_address(self, customer, delivery_address, components, *, label, set_as_default):
        """Write the selected row, else the replaceable provisional row, else a new one."""
        text = format_address(delivery_address)
        address_id = delivery_address.get("address_id")
        if address_id:
            if self._saved_address(customer, address_id) is None:
                raise _AddressUnavailable("I couldn’t find that saved address for your profile. Please choose a saved address or add a new one.")
            addr = update_address_fields(address_id=UUID(str(address_id)), customer=customer,
                                         formatted_address=text, components=components)
            if addr is None:
                raise _AddressUnavailable("That saved address is no longer available. Please choose another address or add a new one.")
            return addr, bool(delivery_address.get("provisional"))
        replaceable = delivery_address.get("replace_address_id")
        if replaceable and self._saved_address(customer, replaceable) is not None:
            addr = update_address_fields(address_id=UUID(str(replaceable)), customer=customer, label=label,
                                         formatted_address=text, components=components,
                                         set_as_default=set_as_default)
            if addr is not None:
                return addr, True
        addr = create_address(tenant=customer.tenant, customer=customer, formatted_address=text,
                              components=components, label=label, set_as_default=set_as_default)
        return addr, True

    def add_delivery_address(self, customer, delivery_address, *, label=None, set_as_default=True):
        return self._save_address(customer, delivery_address, label=label, set_as_default=set_as_default)

    def _extract_updates(self, query):
        # A labelled postcode is already a typed value. Preserve it exactly and
        # let address validation reject invalid values instead of reinterpreting it.
        postcode = re.fullmatch(r'postal code:\s*(\S+)', self.original_query.strip(), re.I)
        if postcode:
            return {'postal_code': postcode[1].strip()}
        result = extract_address_with_gpt(query, original_text=self.original_query,
                                         pending=self.delivery_address,
                                         rephrased_sentence=self.rephrased_sentence)
        # None means extraction failed. An empty dict is a successful no-op.
        if result is None:
            return None
        return normalize_address(result)

    def _merge_updates(self, extracted, delivery_address):
        update_address(extracted, delivery_address)

    def preserve_draft(self, query, delivery_address, checklist, *, new=False):
        """Retain explicit fields before a clarification, without saving or confirming."""
        if new and delivery_address.get('address_id'):
            discard_draft(delivery_address)
        self.delivery_address = delivery_address
        self._merge_updates(self._extract_updates(query), delivery_address)
        checklist["location"] = False

    def update_delivery_address(self, customer, query, delivery_address):
        extracted = self._extract_updates(query)
        if extracted is None:
            return self._ask("I couldn’t read the address details. Please try again.")
        if not extracted:
            # A draft that is incomplete or was never written (for example after a
            # coverage outage) is resubmitted as-is; a saved row needs a change.
            if get_missing_address_keys(delivery_address) or not delivery_address.get("address_id"):
                return self._save_address(customer, delivery_address)
            return self._ask("Please share the address details you want to add or change.")
        self._merge_updates(extracted, delivery_address)
        return self._save_address(customer, delivery_address)

    def existing_addresses(self, customer):
        return self._finish(_list_addresses_response(list_addresses(customer)))

    def delete_delivery_address(self, customer, address_id=None):
        return self._finish("Please delete saved addresses through the app.")

    def set_default_delivery_address(self, customer, query, delivery_address, checklist):
        addresses = list_addresses(customer)
        if not addresses:
            self.sub_intent = "add_delivery_address"
        selected = self._select_address(query, addresses)
        if selected is None:
            return self._ask(_list_addresses_response(addresses) + (
                "\nWhich address should be the default? Please share its label or ID." if addresses else ""))
        addr = update_address_fields(address_id=selected.id, customer=customer, set_as_default=True)
        if addr is None:
            return self._ask("That address is no longer available. Please choose another saved address.")
        self._load_address(addr, delivery_address)
        checklist["location"] = False
        return self._finish(f"Your default address is now {_format_address_line(addr)}.")

    def verify_address_for_delivery(self, customer, query):
        pin = extract_pincode(query)
        if not pin and re.search(r"\b[0-9]{5,}\b", query or ""):
            return self._ask("Please share one valid 6-digit pincode to check delivery coverage.")
        meta = getattr(customer.tenant, "meta", None) or {}
        # A locality-only configuration cannot answer a pincode question.
        if pin:
            verdict = verify_delivery_pincode(customer.tenant, pin)
            area = pin
        else:
            localities = meta.get("serviceable_localities", []) if isinstance(meta, dict) else []
            matches = [loc for loc in localities if isinstance(loc, str) and loc.strip() and re.search(
                rf"(?<!\w){re.escape(loc.strip())}(?!\w)", query or "", re.I)] if isinstance(localities, (list, tuple, set)) else []
            if len(matches) != 1:
                return self._ask("Please share a valid 6-digit pincode or your delivery locality.")
            area = matches[0]
            verdict = verify_delivery_pincode(customer.tenant, area)
        if verdict is True:
            return self._finish(f"Yes, we deliver to {area}!")
        if verdict is False:
            return self._finish(f"Sorry, we don’t currently deliver to {area}.")
        return self._finish("I couldn’t verify delivery coverage right now.")

    def confirm_delivery_address(self, customer, query, delivery_address, checklist, is_followup=False):
        checklist["location"] = False
        extracted = self._extract_updates(query)
        if extracted is None:
            return self._ask("I couldn’t read the address details. Please try again.")
        candidate = dict(delivery_address)
        update_address(extracted, candidate)
        if comparable_address(candidate) != comparable_address(delivery_address):
            self._merge_updates(extracted, delivery_address)
            return self._save_address(customer, delivery_address)
        if get_missing_address_keys(delivery_address):
            return self._save_address(customer, delivery_address)
        coverage = verify_delivery_pincode(customer.tenant, normalize_pincode(delivery_address.get("postal_code")))
        if coverage is False:
            return self._ask("We don’t currently deliver to that pincode. Please choose another address or share a new one.")
        if coverage is not True:
            return self._ask(self.COVERAGE_UNAVAILABLE_RESPONSE)
        saved = self._saved_address(customer, delivery_address.get("address_id"))
        # A complete draft is not necessarily a verified/saved address. In
        # particular a failed correction must never confirm an older DB row.
        if saved is None or comparable_address(saved.components) != comparable_address(delivery_address):
            return self._save_address(customer, delivery_address)
        # Keep the selected delivery address as the customer's default.
        if not saved.is_default:
            saved = update_address_fields(address_id=saved.id, customer=customer, set_as_default=True)
            if saved is None:
                return self._ask("That address is no longer available. Please choose another saved address.")
        checklist["location"] = True
        delivery_address.pop("provisional", None)
        if checklist.get("order", False):
            self.request_handoff("placing_order", sub_intent="order_payment", main_query="order payment",
                                 follow_up_question=["Ready for payment?"])
            return self._finish("")
        return self._finish(f"Delivery address confirmed: {format_address(delivery_address)}.")

    def deny_delivery_address(self, delivery_address):
        discard_draft(delivery_address)
        self.sub_intent = "add_delivery_address"
        return self._ask("Please share the delivery address you’d like to use instead.")

    def _dispatch(self, query, sub_intent, delivery_address, checklist, customer, *, followup=False):
        self.delivery_address = delivery_address
        self.handoff_to, self.handoff_overrides = None, {}
        self.follow_up_question = []
        self.is_complete = False
        if sub_intent not in self.SUB_INTENT_NAMES:
            logger.warning("Unknown location sub-intent: %s", sub_intent)
            return self._finish("I can help you add, choose, or check a delivery address. Please specify what you’d like to do.")
        if customer is None:
            return self._finish("Please sign in or link your customer profile to manage delivery addresses.")
        if str(customer.tenant_id) != str(self.tenant):
            raise ValueError("Customer tenant does not match location intent")
        self.main_query, self.sub_intent = query, sub_intent
        if sub_intent in {"add_delivery_address", "update_delivery_address", "deny_delivery_address"}:
            checklist["location"] = False
        if sub_intent == "add_delivery_address":
            if not followup and delivery_address.get('address_id'):
                discard_draft(delivery_address)
            return self.update_delivery_address(customer, query, delivery_address)
        if sub_intent == "update_delivery_address":
            selected = self._select_address(query, list_addresses(customer))
            if selected is not None and str(selected.id) != delivery_address.get("address_id"):
                self._load_address(selected, delivery_address)
            elif _UUID.search(query or "") and selected is None:
                self.sub_intent = "choose_delivery_address"
                return self._ask("Please choose one of your saved addresses by its label or ID.")
            return self.update_delivery_address(customer, query, delivery_address)
        if sub_intent == "choose_delivery_address":
            return self.choose_delivery_address(customer, query, delivery_address, checklist)
        if sub_intent == "existing_addresses":
            return self.existing_addresses(customer)
        if sub_intent == "delete_delivery_address":
            return self.delete_delivery_address(customer)
        if sub_intent == "set_default_delivery_address":
            return self.set_default_delivery_address(customer, query, delivery_address, checklist)
        if sub_intent == "verify_address_for_delivery":
            return self.verify_address_for_delivery(customer, query)
        if sub_intent == "confirm_delivery_address":
            return self.confirm_delivery_address(customer, query, delivery_address, checklist, followup)
        if sub_intent == "deny_delivery_address":
            return self.deny_delivery_address(delivery_address)

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer):
        self.response = self._dispatch(self.main_query, self.sub_intent, delivery_address, checklist, customer)
        self._sync_checkout_address(customer, delivery_address, checklist)
        return self.response, None

    def _sync_checkout_address(self, customer, delivery_address, checklist):
        if checklist.get('checkout') and self.sub_intent in {
                'add_delivery_address', 'update_delivery_address', 'confirm_delivery_address',
                'deny_delivery_address', 'choose_delivery_address', 'set_default_delivery_address'}:
            from chatbot_core.logic.cafe.checkout import sync_delivery_address
            sync_delivery_address(tenant_id=self.tenant, customer=customer, chat_id=self.chat_id,
                                  platform=self.platform, checklist=checklist, address=delivery_address)

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer):
        self.rephrased_sentence = query_obj.rephrased_sentence
        self.response_language = query_obj.response_language
        self.resolved_action = query_obj.resolved_action
        self.follow_up_reply.append(query_obj.main_query)
        self.original_query = query_obj.original_query
        incoming = query_obj.sub_intent
        # A bare selection/pincode may be classified as an address or a yes.
        # Preserve the operation that asked for it. Explicit denials and
        # independent requests always keep their own meaning.
        if self.sub_intent in {"choose_delivery_address", "set_default_delivery_address", "verify_address_for_delivery"} and incoming in {
            "add_delivery_address", "update_delivery_address", "confirm_delivery_address",
        }:
            incoming = self.sub_intent
        # While an address awaits confirmation, an address-shaped reply is judged
        # by the confirmation: a changed detail is saved and re-confirmed, an
        # unchanged restatement confirms. An explicit update keeps asking for the change.
        if incoming == "add_delivery_address" and self.sub_intent == "confirm_delivery_address":
            incoming = "confirm_delivery_address"
        self.response = self._dispatch(query_obj.main_query, incoming, delivery_address, checklist, customer, followup=True)
        self._sync_checkout_address(customer, delivery_address, checklist)
        return self.response, None
