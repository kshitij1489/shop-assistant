# mock_services/

**Responsible for:** Loopback HTTP simulators (menu, payment, POS, location) and
the reusable reference commerce adapter package.

**Not responsible for:** Shop Assistant business logic, live provider SDKs, or
evaluate scoring.

## Read first

- `__main__.py`, `server.py`, `controls.py`, `client.py`
- `commerce_adapter/`, `location/`

## Docs

- [Local simulators](../docs/mock_services/overview.md)
- [Commerce contract](../docs/commerce/integration.md)

## Verify

```sh
python3 -m mock_services serve   # optional manual
python -m unittest mock_services.test_services mock_services.location.test_location \
  mock_services.commerce_adapter.test_app -v
```
