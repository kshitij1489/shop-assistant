# Evaluation

`evaluate/` checks datasets and can replay website scenarios against the
development stack. `validate` and `score` do not send chat. `run` sends chat
only with `--allow-live-chat`. `transcripts` calls a model unless `--dry-run`
is set.

From the repository root, with Python 3.10 or newer:

```sh
python -m evaluate validate
python -m evaluate validate --require-ready
python -m evaluate run --dataset test_data
python -m unittest discover -s evaluate/tests -v
```

Exit 0 from `validate` means the inputs are structurally valid. It does not
mean a scenario ran or passed. Exit 1 means malformed input. Exit 2 with
`--require-ready` means setup blockers remain. Write evidence outside the
source tree. Do not put secrets, cookies, JWTs, or payment links into artifacts.

## Development stack

Live evaluation uses the development Compose project. There is no separate
evaluation database. Production Compose leaves evaluation disabled.

For the guided setup, run:

```sh
python3 scripts/setup.py
python3 scripts/setup.py demo --allow-live-chat
```

The second command reuses the existing smoke evaluation, including knowledge,
basket, address, pickup/cash, and delivery/online scenarios. Commerce services
are simulated; model calls use `OPENAI_API_KEY` and incur API usage. It waits for
preflight, runs the scenarios, and generates a report. A completed run does not
by itself mean the evaluation passed.

The guided stack uses `.env.demo` and project `shop-assistant-demo`. To target
that same stack with the advanced commands below, first set:

```sh
export APP_ENV_FILE="$PWD/.env.demo"
export COMPOSE_PROJECT_NAME=shop-assistant-demo
export APP_IMAGE=shop-assistant-demo:local
```

For an existing manually configured development stack, keep its environment file,
project, and image instead. Without overrides, `scripts/evaluate-dev` retains its
original `.env.dev` default and development project discovery.

```sh
python3 scripts/evaluate-dev up
python3 scripts/evaluate-dev
```

The launcher uses `.env.dev` unless `APP_ENV_FILE` is set, and it runs commands
inside the development web container. The site stays at `http://localhost:8080`.
Runs create synthetic tenants in that database. Cleanup refuses rows it does
not own. Evidence is on the `evaluation_evidence` volume at
`/var/tmp/studio-eval`.

Preflight checks migrations, adapter TLS, the worker, and a recent scheduler
heartbeat. It does not call a model. Restart web, worker, and scheduler after
Python changes. The development web process uses `--noreload`, so a file edit
cannot restart it during a measured run. Evaluation controls do not flush
development Redis.

## Live run

```sh
python3 scripts/evaluate-dev run --config evaluate/configs/smoke.json \
  --output /var/tmp/studio-eval --allow-live-chat --report
```

Each invocation creates a UUID directory under `--output` and prints the path.
Presets in `evaluate/configs/`:

| File | Shape |
| --- | --- |
| `smoke.json` | Five scenarios, sequential, 40 chat-request cap |
| `acceptance.json` | Selected dataset, sequential, 1,000 request cap |
| `full.json` | Selected dataset, sequential, 2,000 request cap |
| `concurrency.json` | Four concurrent sessions plus warm-up; provider sessions stay serialized |

The preset's `models` must match the running application's model settings. If
you change `LLM_MODEL`, `LLM_TRANSLATE_MODEL`, or `LLM_ANALYTICS_MODEL`, use a
matching config with `python3 scripts/setup.py demo --allow-live-chat --config PATH`
or the advanced runner's `--config` option.

Flags override the preset. A cost ceiling needs an explicit rate. Resume with
the run directory, then score it:

```sh
python3 scripts/evaluate-dev run --config evaluate/configs/smoke.json \
  --output /var/tmp/studio-eval/RUN_UUID --allow-live-chat --resume
python3 scripts/evaluate-dev score /var/tmp/studio-eval/RUN_UUID \
  --output /var/tmp/studio-eval/reports
```

Resume requires compatible code, dataset, and configuration. A finished run
without an evaluator is `COMPLETED`, not `PASS`.

Other commands: `schemas`, `preflight`, `transcripts`, `report`, `inspect`,
`cleanup`. See `python -m evaluate --help`.

## Dataset

`test_data/` holds the evaluation café: knowledge, menu, questions, and session
scripts. `python -m evaluate validate` derives counts from the JSON. Do not
paste session scripts or QA cases into the Knowledge uploader.
For a bulk upload of `test_data/knowledge_base.json`, wrap its object in
`{ "document_type": "knowledge", "documents": ... }`. The evaluation provisioner
wraps its known fixture sources automatically. Demo import files already declare
their types. Publish only after review.

Delivery scenarios use typed street text and tenant coverage. They do not call
a geocoder. Changed prompts or expected answers need a new run.

Do not replay a completed or ambiguous request in its original session. A
timeout after the request was sent is not retried there; a later attempt uses a
new identity. Do not reuse a run directory that already contains `summary.json`,
overwrite saved evidence, or edit a saved report. Missing diagnostics stay
missing. A semantic judgment cannot clear a deterministic failure, and missing
money or cost is unknown rather than zero or a pass. Do not infer success from
polite prose when stored state contradicts it. A payment URL that exists only
in state is not proof it was delivered. Do not seed state to make an assertion
pass. Scenario plans reject path escapes and must not contain Python, shell,
arbitrary HTTP bodies, or executable prose.
