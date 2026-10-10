"""Application-owned request meanings, independent of tenant execution permissions.

Published tenant examples can supply vocabulary. Standard descriptions cannot be
replaced with response instructions; custom FAQ descriptions remain tenant-owned.
"""
from copy import deepcopy


def _request(description, *examples):
    return {'description': description, 'examples': list(examples)}


STANDARD_INTENTS = {
    "general": {
        "cancel_and_abort": _request(
            "Stop the current conversation task, not a placed order. An explicit placed-order "
            "cancellation uses order_enquiry/refund_and_cancellation. If a bare cancel could mean "
            "either the pending task or a placed order, this control lets the workflow clarify the "
            "target.",
        ),
        "goodbye": _request(
            "A farewell or conversation ending; not cancellation of pending work.",
        ),
        "greeting": _request(
            "A standalone greeting or opening salutation.",
        ),
        "small_talk": _request(
            "Casual social conversation without a substantive cafe request.",
        ),
        "thanks": _request(
            "An expression of gratitude; not consent to repeat an action.",
        ),
        "wait": _request(
            "Ask to pause existing work while keeping it pending. Not cancellation, scheduling for "
            "later or a new addition with a deferred choice.",
        ),
    },
    "information_about_the_cafe": {
        "about_the_brand": _request(
            "Ask general questions about the company, brand, reviews or reputation, including "
            "Google ratings.",
            "What is this cafe?",
            "What is your Google rating?",
        ),
        "amenities": _request(
            "Ask about facilities or amenities such as seating, parking or Wi-Fi.",
            "Is there seating?",
            "Can I bring my dog?",
            "Do you have parking or Wi-Fi?",
        ),
        "brand_story": _request(
            "Ask about the origin, history or story of the cafe or brand.",
            "How did the cafe start?",
            "What is the story behind the Ice Cream Factory?",
        ),
        "events_and_tours": _request(
            "Ask about cafe events, factory visits or tours.",
            "Can we visit the ice cream factory with children?",
            "How much is the tasting visit?",
        ),
        "location_and_hours": _request(
            "Ask for the cafe address, directions, map link or opening hours.",
            "Where are you located?",
            "Are you open on Monday?",
            "What time do you close on Sunday?",
        ),
        "team_and_policy": _request(
            "Ask about the team, founders, ownership, legal entity or company policies.",
            "Who founded the business?",
            "What is your ingredient policy?",
        ),
    },
    "insufficient_information": {
        "insufficient_information": _request(
            "The intended request is unclear and needs clarification. Missing execution details or "
            "unavailable facts alone do not make a known intent unclear.",
        ),
    },
    "location_based": {
        "add_delivery_address": _request(
            "Supply or save a new typed delivery address, including partial address-field replies. "
            "Before checkout this is address management; an address requested by open checkout "
            "stays order_confirmation.",
        ),
        "choose_delivery_address": _request(
            "Choose a saved address for delivery. Not confirmation of an already selected address "
            "or setting a default for future orders.",
        ),
        "confirm_delivery_address": _request(
            "Confirm the address in an open address-confirmation question, including after an "
            "information detour. Not checkout confirmation or a new address.",
        ),
        "delete_delivery_address": _request(
            "Ask to delete a saved delivery address. The handler directs the user to the app; this "
            "is not delivery selection.",
        ),
        "deny_delivery_address": _request(
            "Reject the address in an open address-confirmation question; not cancellation of the "
            "order.",
        ),
        "existing_addresses": _request(
            "Ask to list or read saved delivery addresses without selecting or changing one.",
        ),
        "set_default_delivery_address": _request(
            "Set a saved address as the default for future orders; not selecting an address for the "
            "current delivery.",
        ),
        "update_delivery_address": _request(
            "Correct or update a saved delivery address; not the address on a placed order.",
        ),
        "verify_address_for_delivery": _request(
            "Ask whether delivery covers an area, address or pincode. Published delivery fees and "
            "minimums use placing_order/order_channels_and_modes.",
        ),
    },
    "menu_items": {
        "allergens": _request(
            "Ask about allergens, allergy safety or cross-contact; takes precedence over general "
            "dietary preferences. Ingredient questions mentioning allergy, intolerance or "
            "cross-contact stay here even when the recipe is incomplete.",
            "I have a severe nut allergy. What can I safely eat?",
            "Does Chocolate Overload contain wheat?",
            "Does eggless mean safe for an egg allergy?",
            "Which nuts are in Dates Rose & Nuts?",
        ),
        "availability": _request(
            "Ask which menu items or flavors are offered or available; not a request to add them.",
            "Do you have pistachio on the menu?",
            "Is the brownie listed today?",
            "What date-sweetened flavors do you offer?",
        ),
        "dietary_preferences": _request(
            "Ask about dietary suitability or options such as vegan, eggless, dairy-free or "
            "sugar-free. Explicit allergy questions use allergens. Includes no-added-sugar versus "
            "sugar-free, vegetarian, Jain, honey, caffeine and general dietary suitability "
            "questions. Numeric calorie or macro questions use nutrition; explicit allergy safety "
            "uses allergens.",
            "Which ice creams are eggless?",
            "Is dates chocolate sugar-free?",
            "Do you have vegan or Jain options?",
            "Is Coffee Mascarpone vegetarian?",
            "Is anything suitable for someone avoiding added sugar?",
        ),
        "explore_options": _request(
            "Ask to browse the menu, categories or available choices.",
            "Show me the menu.",
            "What categories do you have?",
            "Can I browse the desserts?",
        ),
        "flavor_profile": _request(
            "Ask how an item tastes, including texture or sweetness.",
            "What does Paan & Gulkand taste like?",
            "Banoffee or strawberry, which sounds fruitier?",
            "What is the texture of Chocolate Overload?",
        ),
        "ingredients": _request(
            "Ask what an item contains or about its recipe or composition.",
            "What is in the Banoffee ice cream?",
            "Is there honey in Coconut and Pineapple?",
            "Does your tiramisu have alcohol?",
            "Which spices go into Masala Chai?",
        ),
        "nutrition": _request(
            "Ask about calories, nutrients or nutritional information; dietary labels use "
            "dietary_preferences. Asking for an estimate is still nutrition; do not imply "
            "restaurant-verified values. Suitability labels without nutrient quantities use "
            "dietary_preferences.",
            "About how many calories per 100 g in the brownie?",
            "How much sugar is in the date-sweetened ice cream?",
            "Do you have verified macros for cheesecake?",
        ),
        "pairings": _request(
            "Ask which items or flavors go well together.",
            "What goes with a brownie?",
            "Suggest two contrasting ice cream flavors.",
            "What pairs nicely with cheesecake?",
        ),
        "portion_and_size": _request(
            "Ask about serving sizes, package contents, volume or weight; not choosing a size for a "
            "pending addition.",
            "What sizes are listed for vanilla?",
            "How many brownies are in the pack?",
            "How many ml is a family tub?",
            "How many people does a regular tub serve?",
        ),
        "preparation": _request(
            "Ask how a menu item is made or prepared.",
            "How do you make the vanilla flavor?",
            "Is the pistachio butter made in-house?",
            "What are the layers in Boston Cream Pie?",
        ),
        "pricing": _request(
            "Ask an item or size price, including a corrected item name after a price question. "
            "Mentioning quantity does not request a basket change.",
            "How much is family-size pistachio?",
            "Price of two Tiramisu?",
            "What does the mini tub of Dates Rose & Nuts cost?",
        ),
        "recommendations": _request(
            "Ask for a recommendation, popular item or help choosing what to try.",
            "First time here, what should I try?",
            "I love chocolate and want something eggless.",
            "Something fruity for after dinner?",
        ),
        "source_quality": _request(
            "Ask about ingredient sourcing, freshness or quality.",
            "Where do you get your milk?",
            "Do you use stabilizers?",
            "Does A2 mean lactose-free?",
        ),
        "specialty_items": _request(
            "Ask about signature, premium or specialty menu items.",
            "What are your Originals?",
            "Which dessert is featured?",
            "What makes your chocolate special?",
        ),
    },
    "order_enquiry": {
        "address_or_contact_update": _request(
            "Request an address/contact change for a placed order or ask staff to call a supplied "
            "contact. Store referral; no contact update or callback is performed.",
        ),
        "delivery_problems": _request(
            "Report a late, failed or disputed delivery, or trouble contacting its driver. Store "
            "referral without collecting investigation details.",
        ),
        "general_order_enquiry": _request(
            "General order, delivery or logistics support without a more specific enquiry topic.",
        ),
        "get_order_history": _request(
            "Ask to view or summarize past orders; a read-only enquiry, not a request to reorder.",
        ),
        "missing_or_wrong_items": _request(
            "Report wrong, missing, damaged or melted items, or request a replacement. Store "
            "referral; explicit refunds or placed-order changes use refund_and_cancellation.",
        ),
        "order_status_tracking": _request(
            "Ask for the status or tracking of an existing order or delivery; a read-only enquiry.",
        ),
        "refund_and_cancellation": _request(
            "Request a refund, cancel a placed order or change its items or quantities, including "
            "within a complaint. Store referral; no verified order or receipt is required.",
        ),
    },
    "out_of_context": {
        "out_of_scope": _request(
            "A meaningful request unrelated to the cafe or its services; not a cafe question with "
            "missing facts.",
        ),
    },
    "placing_order": {
        "add_to_basket": _request(
            "Request new items for the basket, including additions with missing choices. Answers to "
            "a pending choice use customize_confirmation.",
        ),
        "cancel_and_abort": _request(
            "Legacy label; use general/cancel_and_abort for stopping pending work. Placed-order "
            "cancellations use order_enquiry/refund_and_cancellation.",
        ),
        "check_order_cart": _request(
            "Ask to view the current basket, its total, summary or changed prices; not consent to "
            "checkout.",
        ),
        "customize_confirmation": _request(
            "Supply a requested item, flavor, size or quantity for a pending basket choice. "
            "Continue that request; not a new addition. Removal target answers use delete_entry.",
        ),
        "delete_entry": _request(
            "Remove an item or reduce its quantity in an unplaced basket, including answers "
            "identifying the item to remove.",
        ),
        "how_to_order": _request(
            "Ask how to order; not a command to add items or begin checkout.",
        ),
        "initiate_order": _request(
            "Start choosing items for a new basket; not checkout. Explicit item additions use "
            "add_to_basket.",
        ),
        "insufficient_information_order": _request(
            "An ordering request whose intended operation is unclear. A known add, edit, removal or "
            "checkout keeps its specific route even when details are missing.",
        ),
        "order_channels_and_modes": _request(
            "Ask about ordering channels, fulfillment modes, delivery fees or minimums; also select "
            "a fulfillment preference before checkout.",
        ),
        "order_confirmation": _request(
            "Start or resume checkout, finish ordering, answer an open checkout field, or confirm "
            "the quoted order. Address confirmation alone is not checkout consent.",
        ),
        "order_payment": _request(
            "Ask about payment or retrieve, resend or retry payment for an existing order. Starting "
            "checkout or supplying an open checkout field uses order_confirmation.",
        ),
        "order_scheduling": _request(
            "Ask to schedule this order for later. Store referral only, including during checkout; "
            "no scheduling action or slot collection.",
        ),
        "payment_confirmation": _request(
            "Report having paid or ask whether payment succeeded. The claim is not proof; payment "
            "must be verified by the handler.",
        ),
        "reorder_or_repeat": _request(
            "Ask to repeat a past order; not an explicit addition of named menu items.",
        ),
        "special_requests": _request(
            "Request a non-catalog customization or special arrangement, including a new item "
            "conditional on it. Store referral only; no basket action or detail collection. "
            "Standard catalog choices stay add/update requests.",
        ),
        "update_order": _request(
            "Change items, flavor, size, or quantity in an unplaced basket. Pending choice answers "
            "use customize_confirmation. Changes to an already placed order use "
            "order_enquiry/refund_and_cancellation.",
        ),
    },
}

KNOWLEDGE_INTENTS = frozenset({'information_about_the_cafe', 'menu_items'})
# Channels also supports SET_FULFILLMENT, so its action permission stays opt-in.
ORDERING_INFORMATION_ROUTES = frozenset({
    ('placing_order', 'how_to_order'), ('placing_order', 'order_channels_and_modes'),
})
PURE_INFORMATION_ROUTES = frozenset({('placing_order', 'how_to_order')})


def standard_schema():
    return deepcopy(STANDARD_INTENTS)


def is_standard_knowledge_route(intent, topic):
    return intent in KNOWLEDGE_INTENTS and topic in STANDARD_INTENTS.get(intent, {})
