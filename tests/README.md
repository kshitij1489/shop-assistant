# tests/

**Responsible for:** Application unit/integration tests, the mock-services harness,
and frontend streaming tests.

**Not responsible for:** Evaluate package tests (`evaluate/`), mock_services
contract unittest modules, or production smoke via Compose (see operations docs).

## Read first

- `settings/integration.py`: offline SQLite tests; no application startup or `.env` loading
- `settings/integration_postgres.py`: real migrations and PostgreSQL concurrency
- `support/`: shared fixtures, provider lifecycle, runner, and reports
- Suite dirs: `unit/`, `integration/`, `framework/`

## Docs

- [Running tests](../docs/operations/testing.md)

## Verify

```sh
python manage.py test tests.integration --settings=tests.settings.integration -v 2
python manage.py test tests.unit --settings=tests.settings.integration -v 2
python -m unittest tests.framework.test_harness -v
node --test tests/frontend/chat_stream.test.cjs
```

Use these same settings for individual test modules. Keep shared fixtures in
`support/`, and extend existing contract tests before adding per-handler copies.

Graph tests using `install_runtime_fixture` or `CheckoutFixture` stub final
presentation with `support.replies.install_reply_renderer`: verified wording and
the selected question pass through unchanged. Other graph fixtures can opt in
explicitly. Classifier and knowledge-provider assertions then measure those calls
only. `unit.test_reply_rendering.ReplyRendererTests` exercises the real renderer
through an offline provider, including protected values and fallback behavior.

Test delivered clarification limits through the conversation graph, not direct
handler calls. Check persistence, progress resets, detours, and terminal outcomes.
Legacy pending tasks may gain `basket_item.clarification_budget` on restoration;
assert that metadata explicitly while preserving assertions on business fields.
Mocks at the normalization provider boundary must use
`NormalizedClassifiedMessages`, including each unit's `rephrased_sentence`.
