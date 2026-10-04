# mock_services/commerce_adapter/

**Responsible for:** Standalone reference adapter (SQLite receipts/outbox, fake
provider, demo CLI) shared with HTTP mock workers.

**Not responsible for:** Django/Studio Desk DB access or certified real PSPs.

## Read first

- Package CLI via `python3 -m mock_services.commerce_adapter`
- `test_app.py`

## Docs

- [Local simulators](../../docs/mock_services/overview.md)
- [Compatibility](../../docs/commerce/compatibility.md)
- [Integration](../../docs/commerce/integration.md)

## Verify

```sh
python3 -m mock_services.commerce_adapter demo
python -m unittest mock_services.commerce_adapter.test_app -v
```
