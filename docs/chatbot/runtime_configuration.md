# Runtime configuration

The Knowledge page saves drafts. **Validate and publish configuration** is what
live conversations use. Saving a draft does not change them.
The page shows the live capability summary, saved documents with unpublished
changes (including deletions), draft validation problems, and a per-topic
comparison with the published values. Publication success means the bundle was
committed; the summary separately reports missing menu/hours facts and disabled
ordering. A failed publication keeps the previous version active.
For an externally managed menu the summary checks the synchronized catalog's
freshness instead of treating uploaded menu documents as current evidence.

`chatbot_core.capabilities.CAPABILITIES` lists the topics the café package can
route. Café information can add FAQ topics. Other capabilities accept only
implemented topics. `order_enquiry/refund_and_cancellation` returns support
information and does not refund. Uploaded knowledge cannot add a new business
workflow. That needs a Python handler in the café package.

New classification imports and edits require an object:

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

Only `description` is required. It describes the customer's request, not how to
write an answer. A description identical to its response instructions blocks
publication. Existing description strings remain readable for compatibility;
convert them to objects when editing or importing them again.

Standard request meanings come from `chatbot_core.intent_definitions`, even for
disabled or missing capabilities. Published tenant examples add vocabulary;
tenant instructions cannot replace the standard meanings. Custom café FAQ
descriptions come only from this tenant's published configuration. Classification
recognizes a request; the workflow separately decides whether it can fulfill it.

Standard café and menu questions use published evidence and safe application
instructions even without an explicit classification document. Missing evidence
produces an explanation of what cannot be verified. Custom FAQs and actions
still require configured routes. `enabled: false` explicitly disables a topic
on the next publication, excluding its knowledge from retrieval too. Deleting
a standard café/menu classification restores the application default; delete
its knowledge as well and publish to retract those facts. Deleting an action or
custom FAQ classification disables that route.

Ordering instructions (`how_to_order`) and questions about channels, modes or
fees (`order_channels_and_modes` with no action) can also read published facts
without enabling transactions. An explicit disable still prevents these answers.
Selecting a fulfillment mode, changing a basket and checking out retain their
action permissions and settings requirements. An informational detour does not
advance an existing checkout.

`required_settings` paths refer to
`TenantInfo.meta`. `required_knowledge` paths are documents in the same tenant.
Catalog reference types are `item`, `category`, `variant`, and `addon`. Category
IDs are integers.

Enabled topics need nonempty response instructions. Café and menu topics also
need nonempty knowledge. Enabled `placing_order` topics other than the purely
informational `how_to_order` need a schema version 2
commerce policy with `ordering_limits` set, even when commerce is disabled.
Checkout routes also need valid Checkout settings. Signup saves complete checkout
and ordering policies first. A new café keeps `placing_order` disabled until its
owner adds a priced menu item and confirms the prefilled setup in Settings. That
action publishes the menu browsing, basket, checkout, address and order enquiry routes together,
without publishing unrelated knowledge drafts. Wait, cancel, and the
insufficient-information fallback stay available.

Publication validates the whole draft and replaces the live documents in one
transaction. A stale form version is rejected. Invalid publication leaves the
live bundle unchanged. The next turn uses the latest published version. Pending
tasks for disabled routes are removed.

Menu prices and availability come from the current catalog, not from published
knowledge text. Generated menu knowledge stays a draft until you publish it.
Checkout settings are read on each checkout call.

## Import types

Use Knowledge for `knowledge_base.json`, Intent Classification for
`intent_classification.json`, and Response Intents for
`response_instructions.json`. Classification strings are rejected by the import
and raw editor so response-instruction files cannot masquerade as routing
descriptions. The supplied demo and test classification files use objects.

Bulk document imports require an explicit type envelope. A missing or mismatched
type is rejected before any documents are saved:

```json
{
  "document_type": "knowledge",
  "documents": {
    "information_about_the_cafe": {
      "location_and_hours": {"hours": "Daily 09:00–18:00"}
    }
  }
}
```

The other document types are `intent_classification` and `response_intents`.
The demo files already include this envelope. For older unwrapped files, put the
original intent/topic object inside `documents` and declare its actual type.
Individual topic editors and internal catalog projections already know the
document type and do not need an envelope. Existing saved documents remain readable.
Document imports update drafts only; review and publish them. Checkout and
ordering-policy imports update live settings immediately. Policy imports preserve
activation flags: limits apply immediately, while policy taxes and discounts apply
only when local policy pricing or external commerce is active. Save Ordering rules
or complete ordering setup to adopt local policy pricing. Ordering limits and checkout settings
must be configured before enabling their dependent ordering routes. A café that
only answers factual questions can keep those actions disabled and publish its
knowledge independently.

## Current opening hours

Hours answers receive a trusted local clock and a computed schedule status from
published `information_about_the_cafe/location_and_hours` knowledge. Provide an
IANA `timezone` and `opening_hours.weekly` with all seven lowercase weekday names;
each day is a list of `{ "opens": "12:00", "closes": "23:30" }` intervals.
An empty list means closed. Overnight intervals and a closing time of `24:00`
are supported. Optional `opening_hours.date_overrides` maps ISO dates to interval
lists; an override owns that whole date, including closures after overnight hours.
Incomplete schedules, invalid zones, and ambiguous DST boundaries produce an
unknown status. Free-form holiday rules require dated overrides for computation.

Answers qualify open/closed as **according to published hours**, not verified live
status; holidays and exceptional closures may differ. The local date and minute
enter the answer cache key, and these answers expire after at most one minute.

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

Retrieval does not change routing and does not authorize an action. Query
expansion is not factual evidence. History can resolve a reference and cannot
authorize repeating an action. A missing or omitted stock row does not mean the
item is sold out.
