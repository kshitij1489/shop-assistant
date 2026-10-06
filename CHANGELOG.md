# Changelog

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
