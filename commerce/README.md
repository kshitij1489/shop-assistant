# commerce/

**Responsible for:** Shared commerce engine, signed adapter protocol, menu sync
authority, readiness, and operator resolution surfaces.

**Not responsible for:** Provider SDKs / merchant onboarding (external adapter
apps), café conversation UX (`chatbot_core`), catalog product CRUD UI (`users/` /
`orders` models).

## Read first

- `services.py`, `api.py`, `events.py`, `menu_sync.py`
- `adapter_client.py`, `readiness.py`

## Docs

- [Integration contract](../docs/commerce/integration.md)
- [Operator runbook](../docs/commerce/operations.md)
- [Menu adapter](../docs/commerce/menu_adapter.md)
- [API compatibility](../docs/commerce/compatibility.md)

## Verify

```sh
python manage.py test tests.integration.test_commerce tests.integration.test_menu_sync \
  --settings=tests.settings.integration_postgres --noinput
```

(Requires disposable Postgres env vars — see [testing](../docs/operations/testing.md).)
