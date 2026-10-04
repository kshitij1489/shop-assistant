# Local setup

Python 3.11, PostgreSQL 15, and Redis 7. Docker Compose is the shortest path.
An OpenAI key is optional for migrate, seed, and smoke checks, and required for
live chat. Native installs also need the spaCy model `en_core_web_sm`.

Copy `.env.example` and replace `SECRET_KEY`, `JWT_SECRET`, and
`POSTGRES_PASSWORD` with separate random values. Do not commit `.env`,
`.env.dev`, `nginx/certs/`, or `celerybeat-schedule*`.

## Compose

From the repository root:

```sh
cp .env.example .env
docker compose -f docker-compose.yml up --build -d
docker compose -f docker-compose.yml logs init
docker compose -f docker-compose.yml exec web python manage.py createsuperuser
```

Seed the fictional demo tenant. The password must be at least 12 characters.
The seed does not enable checkout, payments, or POS.

```sh
read -r -s DEMO_OWNER_PASSWORD
export DEMO_OWNER_PASSWORD
docker compose -f docker-compose.yml exec -e DEMO_OWNER_PASSWORD web python manage.py seed_cafe_demo
unset DEMO_OWNER_PASSWORD
docker compose -f docker-compose.yml exec web python manage.py smoke_installation
```

- Dashboard: `http://localhost:8080/accounts/login/` as `demo-owner`
- Chat: `http://localhost:8080/chat-page/?tenant=demo-cafe`

Put `OPENAI_API_KEY` in `.env`, then run `docker compose -f docker-compose.yml up -d`
again before using chat. `docker compose down` keeps named volumes.

## Dev overlay

The overlay bind-mounts the source and runs `runserver` with `DEBUG=true`. It
is also the live evaluation environment: it adds a location emulator and an
internal HTTPS commerce adapter. Copy `.env.example` to `.env.dev`, set the
same secrets, and start it with the launcher (it selects `.env.dev`):

```sh
python3 scripts/evaluate-dev up --build
python3 scripts/evaluate-dev
```

The second command is a preflight with no model calls. See
[evaluation](../evaluate/integration.md). `APP_ENV_FILE` and Compose `--env-file`
must name the same file. The overlay sets `STUDIO_ENV_FILE=/dev/null` so Django
does not also load a bind-mounted `.env` for missing keys.

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
