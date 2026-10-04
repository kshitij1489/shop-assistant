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

## What a turn guarantees

The graph loads the tenant-scoped session, classifies the message, validates
every proposal, then executes in the customer's order. Handlers enforce catalog,
pricing, and checkout rules. A clarification blocks that proposal. Informational
detours keep unfinished work. The insufficient-information route stops after
two clarification questions.

Checkout commands are short labelled fields (`pickup`, `name: Alice`,
`phone: +44 7700 900123`). The original customer text must confirm the order.
A model rephrase cannot create consent. Explicit dietary or allergy requirements
are stored and restated on later additions and updates. The reply tells the
customer to check with the café. The assistant does not certify allergens or
cross-contact.

Order enquiries are read-only. Status and the latest five orders come from the
tenant and customer records. Refunds, cancellations, and changes to a placed
order return the café's contact response and take no action. The reply includes
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
connection that ends without `done` is a failure. Only the final reply streams.
Proxies must not buffer this response (`X-Accel-Buffering: no`,
`Cache-Control: no-cache`). Each open stream occupies a web worker. Production
secure cookies require HTTPS.
