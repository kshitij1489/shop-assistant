# Changelog

## 1.6.0 — 2026-10-10

- Keep unavailable features from being enabled or used: order scheduling,
  international delivery, WhatsApp chatbot processing, and online payments
  without a ready provider are now blocked consistently across settings,
  imports, and checkout.
- Improve checkout and tenant settings with clearer order limits, grouped
  pricing and advanced options, safer integration forms, and better feedback
  for unsupported or invalid configuration.
- Allow master operators to delete unused tenant setup without removing
  business history; protected records block deletion, and disposable owner
  accounts, onboarding data, and tenant-scoped chat data are cleaned up
  atomically where storage permits.
- Add regression coverage and operational guidance for availability gates,
  tenant deletion, checkout behavior, dashboard settings, and integrations.

## 1.5.0 — 2026-10-10

- Create editable checkout and ordering policies during signup; guide owners to
  add a priced menu item and confirm opening hours before publishing ordering.
- Add dashboard editors for ordering limits, taxes, discounts, and fulfillment
  rules, with currency-aware amounts and validation of local stock readiness.
- Support local policy pricing and atomic stock consumption for cash checkout
  while external payment and POS integrations are disabled.
- Preserve existing policies and activation flags during initialization and
  imports; adopt local policy pricing when saving Ordering rules or completing
  setup, and use the saved checkout coverage after setup.
- Enable cash checkout in the sample café, with local stock, opening hours, and
  delivery coverage; reseeding preserves existing demo edits.
- Recognize standard customer requests independently of tenant execution
  permissions, while keeping custom FAQs and business actions tenant scoped.
- Show published capabilities, missing configuration, validation errors, and
  draft changes on the Knowledge page; failed publication keeps the prior
  configuration active.
- Require typed envelopes for bulk knowledge, classification, and response
  instruction imports; classification edits use objects with descriptions.
- Answer standard café and menu questions from published facts, and keep
  explicitly disabled topics unavailable.
- Compute open/closed answers from published schedules and the café's local
  time, with short-lived caches for time-sensitive answers.

**Upgrade from 1.3.0:** apply database migrations, including commerce migration
`0007_configuration_local_checkout`, before restarting web and background workers.
Deploy the updated application and collected static assets together. Opening
Settings initializes missing policies without replacing saved values. Save
Ordering rules or complete ordering setup to adopt local policy pricing;
existing tenants retain legacy pricing until then. See
[checkout](docs/chatbot/checkout.md) and
[production operations](docs/operations/production.md).

**Configuration imports:** wrap older untyped files with their actual
`document_type` and `documents`; existing saved documents remain readable.
Use the updated demo files as examples. See
[runtime configuration](docs/chatbot/runtime_configuration.md).

## 1.3.0 — 2026-10-09

- Reject TLS certificates for the wrong hostname during production preflight,
  including on systems using OpenSSL 3.0.

- Enforce chatbot rate limits on the deployed endpoint, independently of its URL prefix.
- Serve page scripts as static assets so chat, voice, and dashboard controls work
  with the production Content Security Policy.
- Validate Telegram text and voice delivery responses and apply request timeouts;
  failed sends no longer appear successful in the dashboard.

**Upgrade:** deploy the updated application and collected static assets together,
then restart web and background workers. No new database migration is required
when upgrading from 1.2.0.

## 1.2.0 — 2026-10-09

- Improve dashboard notifications, keyboard navigation, dialogs, and form feedback.
- Keep chat selection, drafts, reading position, and agent controls consistent
  during refreshes; report message delivery and storage failures clearly.
- Improve voice playback, cancellation, message history, and basket display.
- Preserve knowledge drafts, login destinations, and order pagination filters.
- Show saved order currencies, variants, modifier names and prices, and elapsed
  session durations.

**Upgrade:** apply database migrations (including orders migration `0022`) before
restarting web and background workers. Deploy the updated application and
collected static assets together. See
[production operations](docs/operations/production.md) for deployment instructions.

## 1.1.0 — 2026-10-08

- Share validated configuration imports between the dashboard and evaluation
  fixtures; generate knowledge exports from canonical catalog data.
- Make catalog imports atomic, retain existing items during partial updates,
  and save zero quantities correctly.
- Load embedding models on first use to avoid Celery child startup timeouts.
- Improve guided production setup, TLS checks, deployment environment selection,
  and configurable Compose image names.
- Configure Telegram integrations with bot tokens and reject duplicate tokens.
- Preserve supplied address details after pauses and conversational filler,
  including previously supplied street details and original-language values.
- Add configuration, embedding, setup, and address regression coverage, demo
  configuration, and updated deployment documentation.

Restart web and background workers after updating. See
[production operations](docs/operations/production.md) for deployment instructions.

## 1.0.0 — 2026-10-06

First tagged release of Shop Assistant: a configurable café/restaurant chatbot
with menu and knowledge answers, basket management, checkout, commerce adapters,
and local development simulators.

### Improvements

- Basket changes preserve items the customer explicitly asks to keep.
- Address details before checkout follow the address flow; confirming an existing
  draft does not also create a redundant address-selection action.
- Item and generic clarification requests stop after two delivered questions
  without progress. Resolving missing choices resets the limit; unrelated
  questions preserve pending work.
- Final replies use verified execution results, published menu evidence, and the
  selected follow-up. Invalid or failed rendering falls back to verified text.
- Classification uses published intent descriptions and separate contextual
  English rewrites while retaining the customer's reply language.
- Expanded offline regression tests and synthetic multilingual fixtures cover
  basket preservation, address confirmation, routing, and clarification behavior.

### Upgrade notes

- Restart web and background workers after updating application code.
- Website streaming clients must treat `done.response` as authoritative. The
  final composed reply arrives as one `replace` event without token deltas.
- Evaluation datasets now require `intent_classification.json` with descriptions
  for the routes they provision. See [test data](test_data/README.md).
- Setup, deployment, API behavior, adapter limits, and test commands are documented
  in the [documentation index](docs/README.md).

### Scope

This release supports the existing café workflow. Retail and general business
workflows require code changes. Commerce supports one checkout location per
tenant and cash or one online payment attempt per order; split payments and
partial captures are not supported. See [commerce operations](docs/commerce/operations.md).
