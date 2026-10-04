# chatbot_core/logic/cafe/

**Responsible for:** Café intents, basket/checkout, location, and conversation workflow.

**Not responsible for:** Channel HTTP (`chatbot_core/views`), commerce ledger (`commerce/`).

## Read first

- `intent_handler/`, `checkout.py`, `basket.py`, `workflow/`
- `location_utils.py` (text address validation)

## Docs

- [Checkout](../../../docs/chatbot/checkout.md)
- [Catalog ordering](../../../docs/chatbot/catalog_ordering.md)
- [Runtime configuration](../../../docs/chatbot/runtime_configuration.md)
- [Models and conversation](../../../docs/chatbot/llm.md)

## Verify

```sh
python manage.py test tests.integration.test_checkout tests.integration.test_catalog_ordering \
  --settings=tests.settings.integration -v 2
```
