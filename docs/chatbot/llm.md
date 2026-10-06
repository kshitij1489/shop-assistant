# Models and the website chat API

Text and structured calls use `langchain-openai`. Café turns run through the
graph in `chatbot_core/logic/cafe/workflow/`. Set `OPENAI_API_KEY`. Audio uses
the OpenAI audio SDK. Retries apply to provider calls, not to basket, checkout,
or payment operations.

## Configuration

| Variable | When unset |
| --- | --- |
| `LLM_MODEL` | `gpt-4.1-mini` |
| `LLM_TRANSLATE_MODEL` | `gpt-4.1-nano` |
| `LLM_ANALYTICS_MODEL` | `OPENAI_MODEL`, then `gpt-4o-mini` |
| `LLM_TIMEOUT` | 20 seconds |
| `LLM_MAX_RETRIES` | 2 |
| `LLM_MAX_TOKENS` | 2048, or a smaller per-call limit |

`.env.example` sets the three model variables to `gpt-6-luna`. A copied `.env`
uses those values until you change them. For `gpt-6-luna` and `gpt-6-luna-*`
snapshots, requests set `reasoning_effort` to `none`. Restart workers after
changing these settings.

Classification is cached by exact input, tenant, catalog version, prompt, and
model. Empty, refused, or invalid model output fails the turn without saving
basket or checkout changes.

Each live classification also requires a nonblank `rephrased_sentence`: the
self-contained English meaning of that request, including contextual follow-up
answers and unambiguous spelling corrections. Independent requests have separate
rewrites; conditions stay with their operation. `query` retains the customer's
language, and the original message remains available for validation. Protected
names, addresses, identifiers, quantities and negations must survive rewriting.
Older saved decisions without an English rewrite remain readable.

Knowledge retrieval uses the English rewrite for search expansion and retains
the original wording for literal matches. Expansion can reuse a cached result
across languages when the rewrite is identical, within the same tenant and
knowledge version. Answer caching still includes the original query, context,
evidence, rewrite and explicit response language. The rewrite does not determine
the customer's reply language or authorize an action.

## What a turn guarantees

After execution and follow-up selection, `logic/cafe/reply_renderer.py` composes
the final response through one structured LLM call. It receives the original user
message, previous assistant exchange, per-request outcomes and capability facts,
verified handler replies, the permitted follow-up, and the response language.
This shared prompt covers successful results, clarifications, unavailable routes,
rejections, and blocked requests. Handlers still determine business outcomes;
the renderer cannot execute actions, authorize capabilities, or choose new tasks.

The renderer returns both the response and the question actually delivered.
Numbers, URLs, question presence, and the matching question suffix are checked
before publication. The delivered question is saved on its pending task for the
next turn. Provider failures, empty output, or failed validation retain the
original verified reply and question without replaying business operations.
Rendering includes localization, replacing the separate final translation call.
An English fallback can therefore be delivered if rendering fails.

For identified items awaiting a choice or confirmed basket changes,
`logic/cafe/reply_evidence.py` also retrieves published menu context. The renderer
includes documented unit prices and serving-size information (or its explicit
limits) alongside the question or confirmation. A bare unresolved reference has
no identified item and does not trigger this enrichment. Retrieval uses the
tenant-scoped evidence envelope, preserving coverage and freshness; operational
variant labels cannot establish published serving sizes. Missing search results
do not prove that the business publishes no such information.

The original reply's numbers and URLs must remain present. Additional literals
may come only from retrieved factual values, not the customer's query or proposed
action metadata. Basket add/update results include the validated per-unit amount
and currency, including selected modifiers, so the renderer must retain that
amount even when static listing evidence differs or retrieval is unavailable.

The graph loads the tenant-scoped session, classifies the message, validates
every proposal, then executes in the customer's order. Handlers enforce catalog,
pricing, and checkout rules. A clarification blocks that proposal. Informational
detours keep unfinished work. Item and generic clarification tasks stop when a
third question would be needed without progress. One shared counter tracks the
questions selected for delivery across classifier, resolver, and handler paths.
Supplying missing choices resets the counter; handler invocation alone does not.
Checkout field collection and temporary failures are excluded from this limit.
On exhaustion the workflow ends the request, records the limit and the questions
delivered in the turn facts, and the renderer tells the customer what could not
be identified, that nothing changed, and that a new specific request is welcome.
The renderer communicates that decision; it does not make it.

Checkout commands are short labelled fields (`pickup`, `name: Alice`,
`phone: +44 7700 900123`). The original customer text must confirm the order.
A model rephrase cannot create consent. Explicit dietary or allergy requirements
are stored and restated on later additions and updates. The reply tells the
customer to check with the café. The assistant does not certify allergens or
cross-contact. A dietary or ingredient question lists every item on that
explicit list; a label missing from the item name does not drop an item, and
the question does not itself declare a customer restriction. Removals do not
restate a declared requirement. Oversized classification context fails the turn
instead of truncating conditions, and a cache outage does not retry the turn.

Order enquiries are read-only. Status and the latest five orders come from the
tenant and customer records. A receipt id that cannot be found does not fall
back to another order, and an unclear reply cannot select the latest order.
Refunds, cancellations, and changes to a placed order return the café's contact
response and take no action; they do not need an order id. Before checkout,
ordinary basket edits still work. The reply includes
`TenantInfo.meta.support_phone` when it is set.

Website, Telegram, and voice share this runner. The HTTP API below is the
website channel.

## Website chat API

The endpoint authenticates the tenant JWT and uses the Django session cookie
for the browser guest. Each tenant has its own guest customer. Clearing the
browser session starts a new guest. Customer IDs or phone numbers in the
request body do not select an account.

`POST /agent_core/chatbot-api/` accepts `{"message": "..."}` or a form field
named `message` and returns `{"response": "...", "basket": [...]}`. The basket
may be null on a failure fallback.

`Accept: text/event-stream` switches the POST to server-sent events. The chat
page does this.

| Event | Body |
| --- | --- |
| `replace` | `{"text": "..."}` resets provisional text |
| `delta` | `{"text": "..."}` appends text |
| `done` | authoritative `{"response": "...", "basket": [...]}` after the session commits |
| `error` | the turn failed |

Replace provisional text with `done.response`. Do not automatically replay a
failed request; an order or payment may already have been committed. A
connection that ends without `done` is a failure. Café handler drafts are buffered
while the final structured reply is composed and validated. The complete composed
reply is sent as one `replace` event; `done` remains authoritative after commit.
There are no token deltas for this final structured composition.
Proxies must not buffer this response (`X-Accel-Buffering: no`,
`Cache-Control: no-cache`). Each open stream occupies a web worker. Production
secure cookies require HTTPS.
