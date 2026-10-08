# Catalog ordering

Ordering stays off until the tenant saves a schema version 2 commerce policy
with `ordering_limits`. Commerce ceilings in the
[integration contract](../commerce/integration.md) still apply.

Menu JSON imports update items by name and variants by label. Omitted items and
variants remain available until disabled in the dashboard. When a catalog includes
reviewed `knowledge`, generated listings include retained items and variants too.
The optional `pricing.variants_by_item` map is regenerated from catalog prices.
Listed variant and size metadata are retained across partial imports; imported
labels do not establish a verified serving weight or volume. Publish the resulting
knowledge draft to update chatbot answers.

Names the assistant may match:

- `MenuItem.meta["aliases"]`: extra item names. Ingredient tags are not item aliases.
- `MenuItemVariant.aliases`: names for that variant only, edited on the menu item.
  `volume_ml` and `weight_grams` also match. Ounce values are not derived from millilitres.
- **Menu → Manage modifier groups and options:** group and option names, option
  aliases, surcharges, quantity limits, and availability.
- **Menu item → Modifiers:** attach a group, set how many distinct options are
  required, and limit the group to variant UUIDs. An empty variant list applies
  the group to every variant.

Shared group edits affect every attached item. Required choices stay until the
item's minimum is reduced. Options with stock records can be disabled but not
deleted. Existing add-ons start optional, with one selection per group and one
unit per option.

The assistant proposes additions, updates, removals, and replacements. The
server resolves those proposals against the current catalog and applies the
whole proposal or none of it. Unknown variants are rejected. Ambiguous names
ask a question. An unresolved choice does not change the basket.

Mergeable lines share item ID, variant ID, and the same modifier IDs and
quantities. Updates keep omitted fields. `modifiers` null keeps current choices;
`[]` selects the standard configuration when the group rules allow it.

Public basket lines use `currency`, `exponent`, `unit_price_minor` (one unit,
including modifiers), and `line_total_minor`. Commerce `unit_minor` is the base
variant only. Modifier charges are added once. A proposal over a tenant cap is
rejected and the basket is left unchanged. An existing over-limit basket can
still be reduced. Checkout of that basket stays blocked until it is within the
caps. A component price change requires the customer to review the basket
before a new total is charged. Until that review, the unplaced basket keeps its
stored prices. A placed order is not repriced from later catalog changes.

Checkout validates rules and prices again and writes `OrderItemAddon` rows.
The chat does not announce a stock count. A saved selection that no longer
matches a listed size asks which size to use. When commerce stock policy is
strict, confirmation still reserves numeric stock. See [checkout](checkout.md).
