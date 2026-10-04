"""Implemented runtime contracts. Configuration never supplies import paths or code."""
from dataclasses import dataclass
from importlib import import_module


@dataclass(frozen=True)
class Capability:
    module: str
    handler: str
    sub_intents: frozenset[str]
    dynamic_topics: bool = False
    requires_knowledge: bool = False

    def supports(self, topic):
        return self.dynamic_topics or topic in self.sub_intents


CAPABILITIES = {
    "general": Capability(
        "general", "GeneralIntent", frozenset({
            "cancel_and_abort", "goodbye", "greeting", "small_talk", "thanks", "wait",
        }),
    ),
    "information_about_the_cafe": Capability(
        "information_about_the_cafe", "InformationAboutCafeIntent", frozenset({
            "about_the_brand", "amenities", "brand_story", "events_and_tours", "location_and_hours",
            "team_and_policy",
        }), dynamic_topics=True, requires_knowledge=True,
    ),
    "insufficient_information": Capability(
        "insufficient_information", "InsufficientInformationIntent", frozenset({
            "insufficient_information",
        }),
    ),
    "location_based": Capability(
        "location_based", "LocationBasedIntent", frozenset({
            "add_delivery_address", "choose_delivery_address", "confirm_delivery_address",
            "delete_delivery_address", "deny_delivery_address", "existing_addresses",
            "set_default_delivery_address", "update_delivery_address",
            "verify_address_for_delivery",
        }),
    ),
    "menu_items": Capability(
        "menu_items", "MenuItemsIntent", frozenset({
            "allergens", "availability", "dietary_preferences", "explore_options", "flavor_profile",
            "ingredients", "nutrition", "pairings", "portion_and_size", "preparation", "pricing",
            "recommendations", "source_quality", "specialty_items",
        }), requires_knowledge=True,
    ),
    "order_enquiry": Capability(
        "order_enquiry", "OrderEnquiryIntent", frozenset({
            "address_or_contact_update", "delivery_problems", "general_order_enquiry",
            "get_order_history", "missing_or_wrong_items", "order_status_tracking",
            "refund_and_cancellation",
        }),
    ),
    "out_of_context": Capability(
        "out_of_context", "OutOfContextIntent", frozenset({
            "out_of_scope",
        }),
    ),
    "placing_order": Capability(
        "placing_order", "PlacingOrderIntent", frozenset({
            "add_to_basket", "cancel_and_abort", "check_order_cart", "customize_confirmation",
            "delete_entry", "how_to_order", "initiate_order", "insufficient_information_order",
            "order_channels_and_modes", "order_confirmation", "order_payment", "order_scheduling",
            "payment_confirmation", "reorder_or_repeat", "special_requests", "update_order",
        }),
    ),
}

# These routes invoke the implemented checkout, which needs a validated policy.
CHECKOUT_TOPICS = frozenset({"order_payment", "order_confirmation", "order_channels_and_modes", "order_scheduling"})
CONTROL_ROUTES = frozenset({("general", "cancel_and_abort"), ("general", "wait"),
                            ("insufficient_information", "insufficient_information")})


def resolve_handler(name):
    capability = CAPABILITIES.get(name)
    if capability is None:
        raise ValueError(f"Intent '{name}' not registered. Available: {', '.join(CAPABILITIES)}")
    module = import_module(f"chatbot_core.logic.cafe.intent_handler.{capability.module}")
    return getattr(module, capability.handler)
