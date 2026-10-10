# test_data/

**Responsible for:** Café knowledge, menu, QA cases, and session fixtures for evaluation.

**Not responsible for:** Django loaddata; do not paste session/QA JSON into Knowledge upload.

## Dataset provenance

This evaluation dataset uses a dated snapshot of publicly available Dach & Nona
business information. Source links, dates, conflicts, and synthetic content are
recorded in [research_sources.json](research_sources.json). Conversations and
expected answers are synthetic test cases, not customer transcripts. Business
details and prices are test inputs, not current operational information.

For a generic installation, use the fictional `seed_cafe_demo` described in
[local setup](../docs/operations/development.md). Do not import this evaluation
dataset into a live café as its configuration. Keep real customer records,
credentials, and private tenant exports outside the repository.

## Read first

- knowledge_base.json; session_query_sets.json; qa_test_cases.json.
- intent_classification.json: sample intent/sub-intent descriptions aligned with
  the implemented café routes. It uses `existing_addresses`
  instead of the obsolete `read_addresses`. Keep descriptions about user intent;
  they must not promise actions or payment success.

Evaluation provisioning loads this file into `TenantJSONDoc` classification rows
for each scenario's required routes and publishes the configuration. The classifier
embeds the published database descriptions and examples; UI drafts are not live
until published. The file is included in dataset hashes and is separate from the
knowledge inputs. Missing descriptions fail setup instead of falling back to label names.

Ordinary tests use the same descriptions through `tests.support.runtime`.
Additional fixtures can use `evaluate.datasets.loader.classification_documents(root, routes)`
to obtain database-ready documents. String descriptions and objects containing
`description`, optional `examples`, and optional `enabled` are supported.

These are evaluation fixture inputs. For bulk dashboard imports, declare the
actual type and wrap the original object as
`{ "document_type": "knowledge", "documents": ... }` (or `intent_classification`
or `response_intents`). The provisioner wraps its known sources automatically.
The three corresponding files in `demo/` already carry their import types.

## Docs

- [Evaluation dataset](../docs/evaluate/integration.md)
- Package: [../evaluate/README.md](../evaluate/README.md)

## Verify

```sh
python -m evaluate validate --dataset test_data
```
