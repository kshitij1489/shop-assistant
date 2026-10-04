# chatbot_core/

**Responsible for:** Chat channels, sessions, published runtime configuration,
café conversation handlers/workflow, and LLM wrappers.

**Not responsible for:** Commerce ledger/payments (`commerce/`), catalog ORM
ownership (`orders/`), tenant dashboard HTML (`users/`).

## Read first

- `capabilities.py`, `runtime_configuration.py`
- `logic/cafe/` (intents, checkout, basket, location, `workflow/`)
- `llm/` — see scope card there
- `views.py`, `processor.py`, `channels/`

## Docs

- [Runtime configuration](../docs/chatbot/runtime_configuration.md)
- [Checkout](../docs/chatbot/checkout.md)
- [Catalog ordering](../docs/chatbot/catalog_ordering.md)
- [LLM](../docs/chatbot/llm.md)
- [Production tenants](../docs/operations/production.md)

## Verify

```sh
python -m django test tests.unit.test_langchain tests.integration.test_cafe_conversations \
  --settings=tests.settings.integration
python manage.py test tests.integration.test_runtime_configuration \
  --settings=tests.settings.integration --noinput
```
