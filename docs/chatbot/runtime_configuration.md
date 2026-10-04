# Runtime configuration

The Knowledge page saves drafts. **Validate and publish configuration** is what
live conversations use. Saving a draft does not change them.

`chatbot_core.capabilities.CAPABILITIES` lists the topics the café package can
route. Café information can add FAQ topics. Other capabilities accept only
implemented topics. `order_enquiry/refund_and_cancellation` returns support
information and does not refund. Uploaded knowledge cannot add a new business
workflow. That needs a Python handler in the café package.

A classification document may be a description string or an object:

```json
{
  "description": "Questions about bringing pets to the cafe",
  "enabled": true,
  "examples": ["Can I bring my dog?", "Are pets allowed?"],
  "required_settings": ["support.email"],
  "required_knowledge": ["information_about_the_cafe/accessibility"],
  "catalog_references": [{"type": "item", "id": "REPLACE_WITH_ITEM_UUID"}]
}
```

Only `description` is required. `enabled: false`, or deleting the classification,
disables the topic on the next publication. `required_settings` paths refer to
`TenantInfo.meta`. `required_knowledge` paths are documents in the same tenant.
Catalog reference types are `item`, `category`, `variant`, and `addon`. Category
IDs are integers.

Enabled topics need nonempty response instructions. Café and menu topics also
need nonempty knowledge. Enabled `placing_order` topics need a schema version 2
commerce policy with `ordering_limits` set, even when commerce is disabled.
Checkout routes also need valid Checkout settings. A new café publishes
`placing_order` disabled until those limits are set. Wait, cancel, and the
insufficient-information fallback stay available.

Publication validates the whole draft and replaces the live documents in one
transaction. A stale form version is rejected. Invalid publication leaves the
live bundle unchanged. The next turn uses the latest published version. Pending
tasks for disabled routes are removed.

Menu prices and availability come from the current catalog, not from published
knowledge text. Generated menu knowledge stays a draft until you publish it.
Checkout settings are read on each checkout call.

## Knowledge retrieval

Factual café, menu, and informational ordering answers use published `knowledge`
documents for the active tenant. Drafts, settings, classification text, response
instructions, customer data, and order records are not searched. Disabled topics
are excluded. A knowledge document with no classification route can still be
retrieved; delete it and publish to retract it.

Small knowledge sets are supplied whole. Larger sets use a bounded lexical
index. If query expansion fails, retrieval continues without it. Partial
retrieval cannot prove that a fact is absent or that a list is complete.
Negative claims need explicit evidence. Answering a stock question does not
reserve inventory.

For an external menu, uploaded `menu_items` documents are replaced by generated
catalog knowledge. A stale or unavailable menu contributes a status instead of
old uploaded prices. Checkout still revalidates prices and rules on its own.
