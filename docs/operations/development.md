# Local setup

## Guided setup

Install Docker with Compose v2 and Python 3.10+ on the host. PostgreSQL, Redis,
Python application dependencies, and mock services run in containers.

```sh
git clone https://github.com/kshitij1489/shop-assistant.git
cd shop-assistant
python3 scripts/setup.py
```

The launcher asks for an optional OpenAI API key and a demo password (Enter
creates one). It generates the three application/database secrets, writes
`.env.demo` with private permissions, builds the development stack, waits for
readiness, seeds the fictional café, and runs the installation smoke check.
No superuser is needed to explore the demo owner's dashboard.

- Dashboard: `http://localhost:8080/accounts/login/`, username `demo-owner`.
- Password: the one you entered, or `DEMO_OWNER_PASSWORD` in `.env.demo`.
- Chat: `http://localhost:8080/chat-page/?tenant=demo-cafe`.
- First question: “What is on the menu?”

The first build downloads dependencies and models. The sample café supports menu
and information answers; it does not enable checkout, payments, or POS. The
end-to-end evaluation below provisions its own scenarios and synthetic tenants.

An API key is optional for setup, the dashboard, and smoke checks. For live chat,
set `OPENAI_API_KEY` in `.env.demo` and rerun `python3 scripts/setup.py`.
Existing configuration and demo records are preserved; rerunning setup does not
reset an existing owner's password. Never commit these environment files.

## After setup: choose a path

### A. End-to-end demo with mock services

```sh
python3 scripts/setup.py demo --allow-live-chat
```

This starts/refreshes the same local stack, waits for evaluation preflight, then
runs the existing smoke preset and produces a report. Payment/POS interactions
are simulated; chat uses your OpenAI API key and incurs API usage. The preset
selects five scenarios and caps chat requests at 40. Read the execution status
and evaluation verdict in the report: a completed run alone is not a pass.

Evidence stays in the project's `evaluation_evidence` volume under
`/var/tmp/studio-eval/<run-id>`. See [evaluation](../evaluate/integration.md) for
reports, custom configurations, and the advanced runner.

### B. Production deployment

On your server, with DNS and TLS certificates ready:

```sh
python3 scripts/setup.py production
```

Production uses `.env.production` and the `shop-assistant-production` Compose
project. It does not reuse the demo database, install synthetic tenants, or start
mock services. Follow [production setup](production.md) for prerequisites and
operator account creation. You can deploy directly without running the local demo.

## Configuration and troubleshooting

The local launcher uses `.env.demo`, Compose project `shop-assistant-demo`, and
image `shop-assistant-demo:local`. Production uses its own project and image.
The launcher keeps the selected environment file authoritative over unrelated
exported application settings. Configure changes in the file and rerun setup.

- **Docker unavailable:** start your Docker engine and rerun setup.
- **Port 8080 occupied:** change both `HTTP_PORT` and `PUBLIC_URL` in `.env.demo`.
- **Chat unavailable:** check `OPENAI_API_KEY` and the model settings in `.env.demo`.
- **Interrupted setup:** fix the error and rerun; configuration and volumes stay intact.
- **Existing configuration:** use `--env-file PATH --project-name NAME`. For local
  setup, provide `DEMO_OWNER_PASSWORD` (12+ characters), a loopback `PUBLIC_URL`,
  and `HTTP_BIND=127.0.0.1`. Keep the same project name to keep the same volumes.

For unattended local setup, use `--no-input`. New files accept `OPENAI_API_KEY`
and `DEMO_OWNER_PASSWORD` from the environment; missing local secrets are
created automatically. `--configure-only` writes configuration without starting
services. Existing files are never rewritten by either option.

For direct Compose or advanced evaluation commands, select the same stack:

```sh
export APP_ENV_FILE="$PWD/.env.demo"
export COMPOSE_PROJECT_NAME=shop-assistant-demo
export APP_IMAGE=shop-assistant-demo:local
python3 scripts/evaluate-dev                 # preflight only; no model calls
```

To view logs or stop the local stack while retaining its data:

```sh
docker compose --env-file "$APP_ENV_FILE" -f docker-compose.yml -f docker-compose.dev.yml logs --tail=100
docker compose --env-file "$APP_ENV_FILE" -f docker-compose.yml -f docker-compose.dev.yml down
```

The development overlay uses the existing application, PostgreSQL and Redis,
plus a location emulator and internal HTTPS adapter. Source is bind-mounted;
`runserver --noreload` means you must restart after changing Python code.
The older `scripts/evaluate-dev` interface remains available and defaults to
`.env.dev` when `APP_ENV_FILE` is unset.

## Native Python

1. Create a Python 3.11 virtualenv and `pip install -r requirements.txt`.
2. `python -m spacy download en_core_web_sm`. Install FFmpeg if you will test audio.
3. Run PostgreSQL and Redis. Copy `.env.example` to `.env` and point hosts at
   `127.0.0.1`. Set `PUBLIC_URL=http://localhost:8000` and writable `STATIC_ROOT`
   and `HF_HOME`.

```sh
python manage.py check
python manage.py migrate --noinput
python manage.py createsuperuser
python manage.py collectstatic --noinput
python manage.py seed_cafe_demo
python manage.py smoke_installation
python manage.py runserver 127.0.0.1:8000
```

Export `DEMO_OWNER_PASSWORD` before `seed_cafe_demo`. `STUDIO_ENV_FILE` selects
another dotenv path; `/dev/null` disables repo dotenv loading. Process
environment variables win.

## Importing an existing menu

The fictional demo needs no import files. For your own tenant, use the dashboard
menu editor or explicitly select a local knowledge JSON file:

```sh
python manage.py load_menu_items --tenant-id 123 --file /path/to/knowledge_base.json
```

This legacy knowledge format uses `menu_items.availability.all_items` (item
names), `menu_items.pricing` (item → size → price), and optional `menu_category`
and `portion_and_size` maps. It is distinct from the external adapter protocol.
External menus cannot be overwritten by this command. The optional `--slug`
argument selects `tenants/<slug>/knowledge_base.json` instead of `--file`;
these operator-owned files are ignored by Git and excluded from Docker images.
The old customer-specific `load_dummy_orders` command is retired.

## Repository hygiene

Commit application assets under each app's `static/` directory. Django generates
`static/admin/` and `staticfiles/` during `collectstatic`; keep them out of Git.
Uploads, reports, database dumps, credentials, and private tenant imports are
local artifacts. Before publishing changes, run:

```sh
python scripts/check_public_repository.py
```

This checks tracked filenames and common credential patterns. It is a guard
against accidental inclusion, not proof that arbitrary data is safe to publish.
