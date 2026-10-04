"""Ordering business actions; routing and persistence belong to the conversation graph.

Pending item fields live in basket_item so BaseIntent/session round trips retain
clarifications. Mutation results and payment links never pass through an LLM.
"""
import logging
import re
from copy import deepcopy
from urllib.parse import urlsplit
from uuid import UUID

from django.core.exceptions import MultipleObjectsReturned, ObjectDoesNotExist
from django.db import DatabaseError, transaction

from chatbot_core.chat_session import update_chat_session_order
from chatbot_core.logic.cafe.db_utils import create_order
from chatbot_core.logic.cafe.location_utils import format_address, get_missing_address_keys
from chatbot_core.logic.cafe.order_support import store_call_response
from chatbot_core.logic.cafe.prompts.generate_response_from_knowledge import generate_response_from_knowledge
from chatbot_core.logic.cafe.knowledge_context import previous_knowledge_context
from commerce.payment_links import initiate_payment
from orders.models import ChatSession, MenuItem, Order
from chatbot_core.capabilities import CAPABILITIES
from .base import BaseIntent
from chatbot_core.logic.cafe.catalog import positive_integer
from chatbot_core.logic.cafe.ordering_errors import OrderingRejected
from chatbot_core.logic.outcomes import TaskOutcome

logger = logging.getLogger(__name__)


class PlacingOrderIntent(BaseIntent):
    SUB_INTENT_NAMES = CAPABILITIES["placing_order"].sub_intents - {"cancel_and_abort"}
    ITEM_ACTIONS = {"initiate_order", "add_to_basket", "update_order", "delete_entry"}
    STORE_CONTACT_REPLIES = {
        "special_requests": "Sorry, we cannot take special requests through the chatbot. Please contact the store directly for any special requests.",
        "order_scheduling": "Sorry, we cannot schedule orders through the chatbot. Please contact the store directly to arrange a scheduled order.",
    }
    FALLBACK_RESPONSE = "I couldn’t complete that request. Please try again."

    def __init__(self, *, main_query, sub_intent, tenant, chat_id, query_id=0, response="",
                 is_complete=False, ignored_count=0, basket_item=None,
                 follow_up_question=None, follow_up_reply=None):
        super().__init__(main_query=main_query, sub_intent=sub_intent, tenant=tenant,
                         chat_id=chat_id, query_id=query_id, response=response,
                         is_complete=is_complete, ignored_count=ignored_count,
                         basket_item=deepcopy(basket_item or {}),
                         follow_up_question=follow_up_question or [], follow_up_reply=follow_up_reply or [])
        self.intent_type = "placing_order"
        self.promp_restriction = False

    def _finish(self, response):
        return self.set_outcome(TaskOutcome.COMPLETED, response)

    def _reject(self, response):
        return self.set_outcome(TaskOutcome.TERMINAL_REJECTION, response)

    def _block(self, response, *, payment=False):
        if payment:
            # Placement finished checkout, but the payment retry is separate work.
            self.basket_item = {'payment_recovery': True}
            self.sub_intent = 'order_payment'
        return self.set_outcome(TaskOutcome.TEMPORARILY_BLOCKED, response)

    def _ask(self, response):
        attempts = self.basket_item.get("clarification_questions", 0)
        self.basket_item["clarification_questions"] = attempts + 1
        self.response = response
        self.is_complete = False
        self.follow_up_question[:] = [response]
        self.missing_fields = ["selection"]
        return response

    def check_order_cart(self, basket, query, *, api_key, review_prices=True):
        if basket.is_empty():
            return self._finish("Your basket is empty.")
        from chatbot_core.logic.cafe.ordering_limits import basket_subtotal_minor, format_minor, load_policy
        policy = load_policy(tenant_id=self.tenant)
        if policy is None:
            return self._finish("Ordering is unavailable until quantity and amount limits are configured.")
        lines = []
        for row in basket.summary(currency=policy.currency, exponent=policy.exponent):
            modifiers = ""
            if row.get("modifiers"):
                modifiers = " with " + ", ".join(f"{m['quantity']} × {m['name']}" for m in row["modifiers"])
            amount = format_minor(row["line_total_minor"], row["exponent"])
            lines.append(f"{row['quantity']} × {row['name']} ({row['size']}){modifiers}: {row['currency']} {amount}")
        subtotal = format_minor(basket_subtotal_minor(basket.items, policy.exponent), policy.exponent)
        response = "Your basket:\n" + "\n".join(lines) + f"\nItem subtotal: {policy.currency} {subtotal}"
        if review_prices:
            from commerce.menu_sync import assert_menu_fresh
            from commerce.pricing import minor
            from chatbot_core.logic.cafe.catalog import load_catalog, validate_selection, selection_price_changes
            changes = []
            try:
                assert_menu_fresh(self.tenant)
                catalog = load_catalog(api_key)
                for entry in basket.items:
                    current = validate_selection(catalog, entry['item_id'], entry['item_variant_id'],
                                                 entry['quantity'], entry.get('modifiers', []))
                    for change in selection_price_changes(entry, current):
                        old = format_minor(minor(change['previous_unit_price'], policy.exponent), policy.exponent)
                        new = format_minor(minor(change['current_unit_price'], policy.exponent), policy.exponent)
                        component = ('base price' if change['kind'] == 'variant' else
                                     f"{change['name']} ({change['quantity']} per item), price per modifier")
                        changes.append(f"{current['name']} ({current['size']}), {component}: "
                                       f"{policy.currency} {old} → {policy.currency} {new}.")
            except ValueError as exc:
                response += f"\nI couldn’t verify the current selection: {exc} Your basket is unchanged."
            else:
                if changes:
                    response += ("\nCurrent catalog price changes:\n" + "\n".join(changes)
                                 + "\nYour basket still has its previous prices. Review these changes and "
                                   "explicitly update the affected items before checkout. "
                                   "A new quote will require confirmation before placing the order.")
        return self._finish(response)

    def order_payment(self, order, extra_message=""):
        from commerce.models import AcceptedOrder
        record = AcceptedOrder.objects.filter(order=order).first()
        if record and record.state == 'review':
            return self._reject(f'Order {order.id} requires payment or fulfillment review. Please contact the café; do not pay again.')
        if order.payment_status == Order.PaymentStatus.PAID:
            return self._finish(f"Payment for order {order.id} is already confirmed.")
        if order.order_status == Order.Status.CANCELLED:
            return self._reject("That order is cancelled and cannot be paid.")
        if order.payment_mode == 'cash':
            return self._finish(f"Order {order.id} is confirmed. Total: {order.total_amount}. Pay cash at fulfillment.")
        if order.total_amount <= 0:
            return self._reject("I couldn’t verify a payable total for this order. Please contact the café.")
        try:
            info = initiate_payment(order)
            if isinstance(info, dict) and info.get('pending'):
                return self._block('Your order is reserved while the payment provider prepares a secure payment link. Please try payment again shortly.', payment=True)
            path = info.get("payment_url") if isinstance(info, dict) else None
            if not isinstance(path, str) or not path.strip():
                raise ValueError("Missing payment URL")
            parts = urlsplit(path)
            if parts.scheme != "https" or not parts.netloc:
                raise ValueError("Invalid payment URL")
        except Exception:
            # Do not retry a payment provider automatically: its side effect may
            # have succeeded even if the response was lost.
            logger.exception("Payment initiation failed for order_id=%s", order.id)
            return self._block("I couldn’t get a payment link. Your order is saved; please try payment again.", payment=True)
        return self._finish(f"{extra_message}Order total: {order.total_amount}. Here is your payment link: {path}")

    def _checkout_order(self, basket, customer, checklist, *, create):
        """Lock the DB session anchor so a lost Redis save cannot duplicate an order."""
        with transaction.atomic():
            session = (ChatSession.objects.select_for_update().filter(
                tenant_id=self.tenant, session_id=str(self.chat_id),
                platform=self.platform, is_completed=False,
            ).order_by("-last_interaction_at", "-created_at", "-pk").first())
            if session is None or session.customer_id != customer.pk:
                raise ValueError("No active chat session for this customer")
            reference = checklist.get("order_id")
            if reference:
                try:
                    reference = UUID(str(reference))
                except (ValueError, TypeError, AttributeError):
                    raise ValueError("Invalid checkout reference")
                if session.order_id != reference:
                    raise ValueError("Checkout reference does not match this chat")
            if session.order_id:
                order = Order.objects.get(pk=session.order_id, tenant_id=self.tenant, customer=customer)
            elif checklist.get("order") or reference:
                raise ValueError("Missing checkout order")
            elif create:
                # Recheck the database: the menu cache may be stale.
                for entry in basket.items:
                    lookup = {"pk": entry["item_id"]} if entry.get("item_id") else {"name__iexact": entry["name"]}
                    menu_item = MenuItem.objects.select_related("category_fk").get(tenant_id=self.tenant, **lookup)
                    if not menu_item.is_available or (menu_item.category_fk and not menu_item.category_fk.is_active):
                        raise ValueError("Item no longer available")
                order = create_order(customer.tenant, customer, basket, self.chat_id, source="inhouse", payment_mode="cash")
                update_chat_session_order(customer.tenant, self.chat_id, order, platform=self.platform)
            else:
                return None
        checklist.update(order=True, order_id=str(order.id), payment=order.payment_status == Order.PaymentStatus.PAID)
        return order

    def recover_payment(self, basket, customer, checklist):
        """Return the existing chat order's actionable link, never create an order."""
        if (customer is None or str(getattr(customer, 'tenant_id', None)) != str(self.tenant)
                or not self.platform):
            return self._finish('I couldn’t verify a payment for this chat.')
        try:
            order = self._checkout_order(basket, customer, checklist, create=False)
        except (DatabaseError, ObjectDoesNotExist, MultipleObjectsReturned, ValueError):
            logger.exception('Payment recovery lookup failed')
            return self._finish('I couldn’t verify a payment for this chat. Please contact the café.')
        if order is None:
            return self._finish('There is no placed order to pay for in this chat. Please complete checkout first.')
        return self.order_payment(order)

    def order_confirmation(self, basket, customer, delivery_address, checklist):
        if (customer is None or not getattr(customer, "pk", None)
                or str(getattr(customer, "tenant_id", None)) != str(self.tenant) or not self.platform):
            return self._finish("I need your customer details before checkout. Please contact the café for help.")
        if basket.is_empty():
            return self._finish("Your basket is empty. Add an item before checkout.")
        if any(not x.get("name") or not x.get("size") or positive_integer(x.get("quantity")) is None for x in basket.items):
            return self._finish("Your basket has an incomplete or invalid entry. Please correct it before checkout.")
        try:
            order = self._checkout_order(basket, customer, checklist, create=True)
        except (DatabaseError, ObjectDoesNotExist, MultipleObjectsReturned, ValueError):
            logger.exception("Checkout failed for query_id=%s", self.query_id)
            return self._finish("I couldn’t verify this checkout. Please try again or contact the café.")
        if order.payment_status == Order.PaymentStatus.PAID or order.order_status == Order.Status.CANCELLED:
            return self.order_payment(order)
        # An order is a fixed snapshot; do not charge for a different cart after
        # session recovery or an old handler's post-checkout edit.
        expected = sorted((f"{x['name']} ({x['size']})", int(x["quantity"])) for x in basket.items)
        actual = sorted((x.item_name, x.quantity) for x in order.items.all())
        expected_modifiers = sorted((f"{x['name']} ({x['size']})", x["quantity"],
                                     tuple(sorted((str(m["option_id"]), m["quantity"]) for m in x.get("modifiers", []))))
                                    for x in basket.items)
        actual_modifiers = sorted((x.item_name, x.quantity,
                                   tuple(sorted((str(m.addon_id), m.quantity) for m in x.addons.all())))
                                  for x in order.items.prefetch_related("addons"))
        if expected != actual or expected_modifiers != actual_modifiers:
            return self._finish("Your basket differs from the saved order. Please contact the café before paying.")
        if not checklist.get("location") or get_missing_address_keys(delivery_address):
            checklist["location"] = False
            question = ("Please confirm your delivery address." if delivery_address else "Please tell me your delivery address.")
            self.request_handoff("location_based", sub_intent="confirm_delivery_address",
                                 main_query="Confirm delivery address", basket_item={}, follow_up_question=[question])
            return self._finish("")
        try:
            order.location_coordinates = {"address": format_address(delivery_address),
                                          "pincode": delivery_address["postal_code"]}
            order.save(update_fields=["location_coordinates", "updated_at"])
        except DatabaseError:
            logger.exception("Could not save checkout delivery address")
            return self._finish(self.FALLBACK_RESPONSE)
        return self.order_payment(order)

    def configured_checkout(self, basket, customer, checklist, *, reset_address=False, delivery_address=None):
        from orders.models import CheckoutSettings
        from chatbot_core.logic.cafe.checkout import advance_checkout
        from pydantic import ValidationError
        config = CheckoutSettings.objects.filter(tenant_id=self.tenant).first()
        if config is None:
            return None
        if (customer is None or str(customer.tenant_id) != str(self.tenant) or not self.platform):
            return self._finish("I need your customer details before checkout.")
        if basket.is_empty() and not checklist.get('order_id') and not getattr(self, 'checkout_blocker', None):
            if not self.resolved_action or self.resolved_action.proposal.kind in {'CONTINUE_CHECKOUT', 'CONFIRM_ORDER'}:
                return self._finish("Your basket is empty. Add an item before checkout.")
        try:
            reply, order, pending = advance_checkout(
                tenant=customer.tenant, customer=customer, chat_id=self.chat_id, platform=self.platform,
                basket=basket, checklist=checklist, text=self.main_query, original_text=self.original_query, configuration=config.configuration,
                reset_address=reset_address, action=self.resolved_action,
                defer_quote=getattr(self, 'checkout_blocker', None), delivery_address=delivery_address)
        except (ValueError, ValidationError, DatabaseError, ObjectDoesNotExist):
            logger.exception("Configured checkout failed for query_id=%s", self.query_id)
            self.basket_item = {'checkout': True}
            return self._block("I couldn’t validate checkout. Please try again or contact the café.")
        if order:
            if order.payment_mode == 'online':
                return self.order_payment(order)
            if order.order_status == Order.Status.CANCELLED:
                return self._finish("This order has been cancelled.")
            if order.payment_status == Order.PaymentStatus.PAID:
                return self._finish(f"Payment for order {order.pk} is confirmed.")
            return self._finish(f"Order {order.pk} is confirmed. Total: {order.total_amount}. Pay cash at fulfillment.")
        if pending:
            self.basket_item = {'checkout': True}
            draft = checklist.get('checkout', {})
            return self.set_outcome(draft.get('outcome', TaskOutcome.NEEDS_CLARIFICATION), reply)
        return self._finish(reply)

    def _stop_draft(self):
        self.handoff_to = None
        self.handoff_overrides = {}
        self.basket_item = {}
        return self._finish("Alright, I’ve stopped this request. Your basket and any placed order are unchanged.")

    def insufficient_information_order(self, api_key):
        return self._ask("Would you like to add, update or remove an item, view your basket, or check out?")

    def _process(self, basket, delivery_address, checklist, history, api_key, customer):
        self.handoff_to = None
        self.handoff_overrides = {}
        self.response = ""
        self.sub_intent = self.sub_intent.strip().lower() if isinstance(self.sub_intent, str) else ""
        if self.sub_intent not in self.SUB_INTENT_NAMES:
            return self._finish("I can help add, update or remove basket items, or help you check out.")
        if self.sub_intent in self.STORE_CONTACT_REPLIES:
            self.basket_item = {}
            return self._reject(self.STORE_CONTACT_REPLIES[self.sub_intent])
        if not isinstance(self.main_query, str) or not self.main_query.strip():
            return self.insufficient_information_order(api_key)
        if self.basket_item.get('payment_recovery') or (self.resolved_action and self.resolved_action.proposal.kind == 'RECOVER_PAYMENT'):
            return self.recover_payment(basket, customer, checklist)
        if (self.resolved_action and self.resolved_action.proposal.kind == 'SET_FULFILLMENT'
                and not checklist.get('checkout')):
            from orders.models import CheckoutSettings
            from chatbot_core.logic.cafe.checkout import remember_fulfillment_preference
            config = CheckoutSettings.objects.filter(tenant_id=self.tenant).first()
            try:
                mode = remember_fulfillment_preference(checklist, self.resolved_action.proposal.value,
                                                      config.configuration if config else None)
            except ValueError as exc:
                return self._reject(str(exc))
            return self._finish(f'{mode.title()} preference saved. Say checkout when you are ready to review your order.')
        from chatbot_core.logic.cafe.checkout import START_CHECKOUT
        checkout_topic = self.sub_intent in {"order_payment", "order_confirmation", "order_channels_and_modes", "order_scheduling"}
        if self.basket_item.get('checkout') or (checkout_topic and (
                checklist.get('checkout') or self.sub_intent == 'order_confirmation')):
            response = self.configured_checkout(basket, customer, checklist, delivery_address=delivery_address)
            if response is not None:
                return response
        if self.sub_intent in self.ITEM_ACTIONS:
            if checklist.get("order") or checklist.get("order_id"):
                return self._reject(store_call_response(tenant_id=self.tenant))
            resolved = self.resolved_action
            if resolved and resolved.proposal.kind == 'CHANGE_BASKET':
                from chatbot_core.logic.cafe.order_changes import apply_proposal
                from pydantic import ValidationError
                self.basket_item.setdefault("original_request", self.original_query)
                self.basket_item.setdefault("replies", []).append(self.original_query)
                self.basket_item["action_proposal"] = resolved.proposal.model_dump()
                self.basket_item["proposal"] = resolved.proposal.basket.model_dump()
                before = {row['item_number']: deepcopy(row) for row in basket.items}
                try:
                    response = apply_proposal(resolved, basket, api_key,
                                              declared_constraints=checklist.get('declared_constraints', []))
                except OrderingRejected as error:
                    self.basket_item = {}
                    return self._reject(f'{error} Your basket is unchanged.')
                except DatabaseError:
                    logger.exception('Basket change temporarily failed for query_id=%s', self.query_id)
                    return self._block('I couldn’t apply that basket change. Please try again shortly.')
                except (ValueError, ValidationError) as error:
                    message = ("Please specify the item, size and customizations." if isinstance(error, ValidationError) else str(error))
                    return self._ask(message)
                changed = [row['item_number'] for row in basket.items if before.get(row['item_number']) != row]
                # Only successful, unambiguous work establishes focus. Retain a
                # removed ID as a stale reference rather than selecting another row.
                removed = set(before) - {row['item_number'] for row in basket.items}
                affected = set(changed) | removed
                checklist['basket_focus'] = next(iter(affected)) if len(affected) == 1 else None
                self.basket_item = {}
                return self._finish(response)
            return self._ask('Please specify the item, size and customizations.')
        if self.sub_intent == "check_order_cart":
            return self.check_order_cart(basket, self.main_query, api_key=api_key,
                                         review_prices=not (checklist.get('order') or checklist.get('order_id')))
        if self.sub_intent in {"order_payment", "order_confirmation"}:
            from orders.models import CheckoutSettings
            if CheckoutSettings.objects.filter(tenant_id=self.tenant).exists():
                return self._finish('Say checkout to review your order and payment details.')
            return self.order_confirmation(basket, customer, delivery_address, checklist)
        if self.sub_intent == "payment_confirmation":
            if customer is None or str(getattr(customer, "tenant_id", None)) != str(self.tenant):
                return self._finish("I couldn’t verify a payment for this chat.")
            try:
                order = self._checkout_order(basket, customer, checklist, create=False)
            except (DatabaseError, ObjectDoesNotExist, MultipleObjectsReturned, ValueError):
                logger.exception("Payment status lookup failed")
                order = None
            if order and order.payment_status == Order.PaymentStatus.PAID:
                return self._finish(f"Payment for order {order.id} is confirmed.")
            return self._finish("I haven’t verified a completed payment for this chat yet.")
        if self.sub_intent == "insufficient_information_order":
            return self.insufficient_information_order(api_key)
        if self.sub_intent == "customize_confirmation":
            return self._finish("There is no pending item to confirm. Please specify the item, size and quantity.")
        if self.sub_intent == "reorder_or_repeat":
            return self._finish("I can’t apply that request automatically. Please specify standard menu items to add, or contact the café for help.")
        response = generate_response_from_knowledge(
            api_key, self.sub_intent, self.main_query, main_intent=self.intent_type, promp_restriction=self.promp_restriction,
            rephrased_sentence=self.rephrased_sentence, response_language=self.response_language,
            **previous_knowledge_context(self, history),
            response_profile="ordering_information",
            fallback_response="I couldn’t verify that ordering detail from the available information.",
        )
        return self._finish(response if isinstance(response, str) and response.strip() else self.FALLBACK_RESPONSE)

    def process_query(self, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        return self._process(basket, delivery_address, checklist, history, api_key, customer), None

    def process_followup(self, query_obj, basket, delivery_address, checklist, history, api_key, customer) -> tuple[str, int | None]:
        self.rephrased_sentence = query_obj.rephrased_sentence
        self.response_language = query_obj.response_language
        self.original_query = query_obj.original_query
        self.resolved_action = query_obj.resolved_action
        incoming = query_obj.sub_intent.strip().lower() if isinstance(query_obj.sub_intent, str) else ""
        arguments = (basket, delivery_address, checklist, history, api_key, customer)
        if (self.basket_item.get('checkout') and str(query_obj.tenant) == str(self.tenant)
                and query_obj.chat_id == self.chat_id and query_obj.platform == self.platform):
            continuation = ((query_obj.intent_type == 'placing_order' and incoming in {
                'order_confirmation', 'order_payment', 'order_channels_and_modes', 'order_scheduling', 'customize_confirmation'})
                or (query_obj.intent_type == 'location_based' and incoming in {
                    'confirm_delivery_address', 'change_delivery_address', 'deny_delivery_address',
                    'add_delivery_address', 'update_delivery_address'}))
            if not continuation:
                return self._ask(self.get_followup_question() or 'Please continue checkout.'), None
            self.main_query = query_obj.main_query
            reset_address = query_obj.intent_type == 'location_based' and incoming in {
                'deny_delivery_address', 'change_delivery_address', 'update_delivery_address',
                'add_delivery_address'}
            if (query_obj.intent_type == 'location_based' and incoming != 'deny_delivery_address'
                    and checklist.get('checkout', {}).get('mode') == 'delivery'):
                from chatbot_core.logic.cafe.workflow.actions import explicit_checkout_action
                explicit = explicit_checkout_action(query_obj.main_query)
                if explicit and explicit.kind == 'SET_CHECKOUT_FIELD':
                    reset_address = explicit.field == 'address'
                elif not re.fullmatch(
                        r'(?:please\s+)?(?:change|update|replace|add)(?:\s+(?:my|the|delivery|an?|new))*\s+address[.!]?',
                        query_obj.main_query.strip(), re.I):
                    from chatbot_core.logic.cafe.intent_handler.location_based import update_address
                    from chatbot_core.logic.cafe.checkout import sync_delivery_address
                    # Checkout owns the pending task, but extraction still needs
                    # the complete address context when the reply is just a pin.
                    query_obj.delivery_address = delivery_address
                    extracted = query_obj._extract_updates(query_obj.main_query)
                    if extracted is None:
                        return self._ask('I couldn’t read the address details. Please try again.'), None
                    if extracted:
                        update_address(extracted, delivery_address)
                        checklist['location'] = False
                        sync_delivery_address(tenant_id=self.tenant, customer=customer, chat_id=self.chat_id,
                                              platform=self.platform, checklist=checklist, address=delivery_address)
                    reset_address = reset_address and not extracted
                    self.main_query = 'checkout'
            if reset_address:
                delivery_address.clear()
                checklist['location'] = False
                # An address-management request is a command, not the value
                # of whichever checkout field happens to be awaiting a reply.
                # Require an explicit address field to provide a replacement.
                self.main_query = 'checkout'
                if incoming != 'deny_delivery_address' and re.fullmatch(
                        r'address:\s*\S.*', query_obj.main_query.strip(), re.I):
                    self.main_query = query_obj.main_query
            elif query_obj.intent_type == 'location_based':
                # A bare address answer is only eligible for the address slot.
                # Never let it fill contact details or a dine-in table.
                if (checklist.get('checkout', {}).get('awaiting') != 'address'
                        and not re.fullmatch(r'postal code:\s*\S.*', self.main_query.strip(), re.I)):
                    self.main_query = 'checkout'
            return self.configured_checkout(basket, customer, checklist,
                                            reset_address=reset_address, delivery_address=delivery_address), None
        if (query_obj.intent_type == "insufficient_information" and str(query_obj.tenant) == str(self.tenant)
                and query_obj.chat_id == self.chat_id and query_obj.platform == self.platform):
            return self._ask(self.get_followup_question() or "Please specify the menu item and size."), None
        if (str(query_obj.tenant) != str(self.tenant) or query_obj.chat_id != self.chat_id
                or query_obj.platform != self.platform or query_obj.intent_type != self.intent_type):
            return self._ask("Please continue this request in its original chat."), None
        # Informational interruptions cannot be treated as values for a pending
        # mutation. The graph will emit this answer and repeat the saved question.
        continuations = self.ITEM_ACTIONS | {"customize_confirmation", "insufficient_information_order"}
        controls = {"order_payment", "order_confirmation"}
        if incoming not in continuations | controls and not self.is_complete:
            return query_obj.process_query(*arguments)
        if incoming in {"order_payment", "order_confirmation"} and self.sub_intent in self.ITEM_ACTIONS and any(
                value is not None for value in self.basket_item.values()):
            return self._ask("Please finish or cancel the pending basket change before checkout."), None
        if incoming == "delete_entry" and self.sub_intent != "delete_entry":
            self.basket_item = {}
            self.sub_intent = incoming
        self.main_query = query_obj.main_query
        self.follow_up_reply.append(query_obj.main_query)
        if self.sub_intent in {"order_payment", "order_confirmation"} and incoming == "customize_confirmation":
            # This intent was explicitly queued by the address handler. A yes
            # resumes checkout; an arbitrary clarification must not trigger it.
            if not isinstance(self.main_query, str) or not re.fullmatch(
                    r"\s*(yes|yes please|ok|okay|sure|confirm|proceed|pay|ready)[.!]?\s*", self.main_query, re.I):
                if isinstance(self.main_query, str) and re.fullmatch(r"\s*(no|no thanks|stop|cancel)[.!]?\s*", self.main_query, re.I):
                    return self._stop_draft(), None
                return self._ask("Would you like to proceed to payment?"), None
        elif self.sub_intent not in self.ITEM_ACTIONS or incoming in controls:
            self.sub_intent = incoming
        if incoming == "insufficient_information_order" and self.sub_intent in self.ITEM_ACTIONS:
            return self._ask(self.get_followup_question() or "Please specify the item, size and quantity."), None
        return self.process_query(*arguments)
