# test_data/

**Responsible for:** Café knowledge, menu, QA cases, and session fixtures for evaluation.

**Not responsible for:** Django loaddata; do not paste session/QA JSON into Knowledge upload.

## Read first

- knowledge_base.json; session_query_sets.json; qa_test_cases.json.

## Docs

- [Evaluation](../docs/evaluate/integration.md)
- Package: [../evaluate/README.md](../evaluate/README.md)

## Verify

```sh
python -m evaluate validate --dataset test_data
```
