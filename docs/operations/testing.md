# Tests

Settings under `tests.settings` do not load `.env`. Use `integration` for SQLite
and `integration_postgres` for migrations and PostgreSQL. Reports go to
`.test-reports/` unless you pass `--report-dir=`. Do not point these settings
at a production database.

CI (`.github/workflows/installation.yml`) uses empty PostgreSQL and Redis:
migrate, check migration drift, collect static files, seed twice, run the
ownership audit and smoke check, then the suites below. It does not call live
payment providers.

## Installation

Guided setup and deployment gates (standard library only; no Docker or API calls):

```sh
python3 -m unittest tests.framework.test_setup tests.integration.test_production_start -v
```

Disposable PostgreSQL and Redis. Django creates a `test_` database, so the role
needs `CREATEDB`. Use an isolated Redis database.

```sh
python manage.py test \
  tests.integration.test_installation \
  tests.integration.test_installation_settings \
  tests.integration.test_production_start --noinput
```

## SQLite

```sh
python manage.py test tests.integration --settings=tests.settings.integration -v 2
python manage.py test tests.unit --settings=tests.settings.integration -v 2
node --test tests/frontend/chat_stream.test.cjs
```

`--parallel` greater than 1 is rejected.

For opt-in live checks of “remove X, keep Y”, run the synthetic multilingual
fixture with the configured model. This calls the provider using `.env.dev`;
results include typed actions and resolved basket targets. Offline tests alone
do not establish language interpretation accuracy.

```sh
python scripts/evaluate_contextual.py --live --workers 1 \
  --cases tests/fixtures/basket_preservation.json \
  --output /tmp/basket-preservation-live.json
```

For address confirmation, this synthetic fixture checks that confirming and
saving the same address produces one operation, while corrections, denials and
independent requests keep their meaning:

```sh
python scripts/evaluate_contextual.py --live --workers 1 \
  --cases tests/fixtures/address_confirmation.json \
  --output /tmp/address-confirmation-live.json
```

## PostgreSQL commerce

Set `COMMERCE_TEST_PG_HOST` (default `127.0.0.1`), `COMMERCE_TEST_PG_PORT`
(default `5432`), `COMMERCE_TEST_PG_USER`, and `PGPASSWORD`. Django creates
`test_studio_desk_commerce`.

```sh
python manage.py test \
  tests.integration.test_commerce \
  tests.integration.test_commerce_operations \
  tests.integration.test_menu_sync \
  tests.integration.test_reference_adapter \
  tests.integration.test_mock_commerce \
  tests.integration.test_checkout \
  --settings=tests.settings.integration_postgres --noinput -v 2
```

## Other packages

```sh
python -m unittest tests.framework.test_harness -v
python -m unittest mock_services.test_services mock_services.location.test_location \
  mock_services.commerce_adapter.test_app -v
python -m evaluate validate
python -m unittest discover -s evaluate/tests -v
```

Do not select both `mock_services.test_engine` and
`tests.integration.test_mock_commerce` in one run. `validate` does not send
chat. `python -m evaluate run` sends chat only with `--allow-live-chat`. See
[evaluation](../evaluate/integration.md).
