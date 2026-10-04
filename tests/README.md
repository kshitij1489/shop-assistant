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
