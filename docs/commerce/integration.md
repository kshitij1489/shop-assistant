# Commerce integration

Shop Assistant keeps the catalog, orders, accepted snapshots, and external mappings.
An adapter is a separate application. It does not need this database. Provider
SDKs and merchant onboarding stay in the adapter. This repository does not
include certified Square, Clover, Toast, Adyen, or Mollie clients.

Read [recovery](operations.md) before enabling live orders. The local walkthrough
is [mock services](../mock_services/overview.md). Version rules are in
[compatibility](compatibility.md). Menu import is in [menu adapter](menu_adapter.md).

## Limits

- One checkout location per tenant.
- One active connection per role and location. Payment and POS may use different providers.
- One currency. No FX. Supported exponents: 2 for INR, EUR, GBP, USD, CAD, AUD, CHF, SEK, NOK, DKK, PLN, and CZK; 0 for JPY. Three-decimal currencies are rejected.
- Cash, or one online payment attempt, per accepted order. Online fulfillment needs an exact, unrefunded full capture before expiry. No partial captures, split tenders, gift cards, or tips.
- Whole-unit quantities. No weighted items.
- One eligible discount code. No compound taxes and no mixed inclusive/exclusive taxes.
- Strict stock is the default: a numeric count is required for every selected item or variant. Availability-only and untracked policies cannot promise overselling protection. A local reservation does not lock an independent POS.

Use PostgreSQL in production. Snapshots are immutable through the model API and
a database trigger. Do not change the ledger by SQL.

## Adapter database

Keep at least these tables in the adapter:

| Table | Uniqueness and contents |
| --- | --- |
| connection | Shop Assistant connection UUID, merchant account, environment, secret reference |
| entity_mapping | connection, kind, canonical ID, external ID, scope; unique both ways |
| command_receipt | command UUID, exact request, request hash, provider idempotency key, outcome |
| provider_inbox | provider event ID unique within the account |
| object_state | provider resource ID and last authoritative state |
| event_outbox | stable event ID and exact normalized payload |
| stock_acknowledgement | reservation UUID and the observation that includes that sale |

Do not send card numbers, CVVs, credentials, or bearer tokens to this API.

## Authentication

Base URL: `/commerce/v1/connections/{connection_uuid}/`. TLS is required.

Create a connection under **Commerce settings → Provider connections**. The
signing secret is shown once. Managed secrets are HMAC-SHA256 derived from
Django's `SECRET_KEY`, the connection and location IDs, and a rotation nonce.
Rotating the nonce or `SECRET_KEY` invalidates the credential immediately.

Deployment-managed secrets remain supported:

```text
COMMERCE_ADAPTER_SECRETS={"my-adapter-key":"<random-secret>"}
```

Set `Connection.secret_ref` to `my-adapter-key`.

```text
X-Commerce-Timestamp: <Unix seconds>
X-Commerce-Signature: <lowercase hex HMAC-SHA256>
```

Sign these bytes with LF separators and no trailing LF:

```text
<timestamp>\n<UPPERCASE_METHOD>\n<path including query string>\n<raw request body>
```

Empty GET bodies are `b''`. Timestamp tolerance is five minutes. Re-sign retries
with a new timestamp and the same event ID and body. The reference implementation
is `commerce.api.signature`. The request limit is 256 KiB. The adapter must
verify the provider's own signature, or fetch the resource from the provider,
before reporting a payment. A browser redirect is not proof of payment.

`commerce/adapter_client.py` is a standard-library client:
`AdapterClient("https://your-host/commerce", connection_id, secret)`.

## Endpoints

Paths are relative to the connection base URL.

| Method and path | Purpose |
| --- | --- |
| `GET manifest/` | Account, capabilities, location, pricing policy, menu generation |
| `GET schema/` | JSON Schemas for events, acknowledgements, mappings, menu snapshots, commands, and claim responses |
| `GET events/{event_id}/` | Processing status of a received event |
| `GET catalog/?offset=0` | POS catalog export, 100 items per page |
| `POST catalog/snapshot/` | Complete external menu import |
| `GET stock/?offset=0` | Stock for this POS authority, 100 per page |
| `GET mappings/?offset=0` | Mappings, 200 per page |
| `POST mappings/` | Idempotent mapping; rebinding is rejected |
| `GET orders/{order_uuid}/` | Accepted snapshot and current state |
| `POST commands/claim/` | Lease up to 20 due commands for 120 seconds |
| `POST commands/{command_uuid}/ack/` | Acknowledge the current lease |
| `POST events/` | Normalized provider observation |

Offset exports are bootstrap reads, not a change feed. Advertise only
capabilities the adapter implements. POS: `order.submit`, `order.reconcile`,
`inventory.update`, `catalog.read`, and optionally `catalog.write`. Payment:
`payment.create`, `payment.reconcile`, and optionally `payment.refund`. A
menu-only connection may advertise only `catalog.write`.

Checked-in schemas: [command.schema.json](../../commerce/contracts/command.schema.json)
and [claim_response.schema.json](../../commerce/contracts/claim_response.schema.json).

Command types are `payment.create`, `payment.reconcile`, `payment.refund`,
`order.submit`, and `order.reconcile`. `command_id` and `idempotency_key` stay
stable. `lease_token`, `lease_until`, and `attempt` may change; do not include
lease fields in the provider request hash. Look up an existing provider resource
with the original create key, never with a reconciliation command's key.

`payment.refund.data.target_refunded_minor` is the desired cumulative refunded
total. Refund only the remaining difference. Persist the provider idempotency
key before calling the provider.

Acknowledgement outcomes: `succeeded`, `retry`, `unknown`, `failed`. Use `retry`
only when the same provider key is safe to repeat. A timeout after a possible
side effect is `unknown` unless the adapter can deduplicate. Ten failed
deliveries or expired leases park the command. Persist a receipt before provider
I/O.

## Events

Schema version 1, stable `event_id`, timezone-aware `occurred_at`, and a
`sequence` that increases per provider resource. Sequences come from
authoritative observations, not webhook arrival order.

Payment statuses: `pending`, `authorized`, `captured`, `failed`, `cancelled`,
`refunded`. Captured and refunded amounts are cumulative and cannot decrease.
Authorization is not capture. A pending update may include an HTTPS
`checkout_url`. Only an exact unrefunded capture, while the order is awaiting
payment and before expiry, marks it paid. Late or mismatched captures stay in
review, release held stock, and queue `payment.refund` when that capability
exists and money remains. Connections without refund support need manual
reconciliation.

`inventory.updated` carries stock ID, sequence, `observed_at`, integer
`on_hand`, `available`, and acknowledged reservation UUIDs. Acknowledge a
reservation only when that same count already includes the sale. Available to
reserve is `on_hand - reserved - pending_consumed`.

`order.updated` statuses: `accepted`, `preparing`, `dispatched`, `delivered`,
`rejected`, `cancelled`. State does not move backward through fulfillment.
Rejection does not imply a refund or a restock.

Response `200` means processed. `202` means stored and queued after a processing
failure. `400` is a bad or cross-capability request. `401` is unauthenticated.
Identical duplicate events are no-ops. Reusing an event ID with different
content is rejected. Inbox retries stop after ten attempts.

## Price

Wire money is a nonnegative integer in minor units, with currency and exponent.
Line `unit_minor` is the base variant only. Modifier unit prices round half up
separately. Modifier quantities are per purchased item, and modifier subtotals
include the parent quantity. The line subtotal includes base and modifier
charges once. One discount applies before tax. Fixed discounts use
largest-remainder allocation. Packaging and fulfillment are separate fee lines.
Taxes round per line. The stored pricing policy is schema version 2.
`ordering_limits` stays null until every cap is set; that alone does not enable
ordering. If a POS cannot reproduce the accepted total, flag the order. Do not
charge a different amount.

## Rollout

1. `python manage.py migrate`. Existing tenants are not enabled automatically.
2. Save checkout settings and a commerce policy at `/commerce/settings/`. Fees stay in Checkout settings.
3. Save commerce settings while commerce is disabled to create the location. Add provider connections and stock. External stock starts at zero until an adapter event arrives. Connection identity is immutable after creation.
4. Deploy adapters. Use provider sandbox accounts. Start command pollers and webhook receivers.
5. Activate tested connections and enable commerce. The dashboard rejects incomplete activation. Run Celery worker and beat. Beat runs `commerce.tasks.reconcile_commerce` about every 60 seconds, with provider reads grouped in five-minute buckets. Or run `python manage.py reconcile_commerce` every minute.
6. Watch failed commands, inbox rows, stale stock, and open reconciliation issues.

A POS command is sent only after cash confirmation or a verified full online
capture.

For a batch, preview then apply:

```sh
python manage.py configure_commerce --tenant 12 \
  --checkout-policy /path/to/checkout.json --commerce-policy /path/to/commerce.json
python manage.py configure_commerce --tenant 12 --enable --apply
```

Without `--apply`, changes roll back. `--enable` runs only after adapter and
stock setup. Every selected tenant must be approved. A failed activation rolls
back the batch. Publish online checkout only after cash checkout is saved,
commerce is enabled, and the payment adapter is active.
