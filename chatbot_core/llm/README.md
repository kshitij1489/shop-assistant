# chatbot_core/llm/

**Responsible for:** LangChain/OpenAI model wrappers, chains, structured schemas,
and related caching behavior used by café handlers.

**Not responsible for:** Graph routing (`logic/cafe/workflow/`), channel HTTP,
or live answer-quality evals (`evaluate/`).

## Read first

- `models.py`, `chains.py`, `schemas.py`

## Docs

- [LLM design](../../docs/chatbot/llm.md)
- [Runtime configuration](../../docs/chatbot/runtime_configuration.md)

## Verify

```sh
python -m django test tests.unit.test_langchain --settings=tests.settings.integration
```
