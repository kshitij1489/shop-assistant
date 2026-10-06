# Production

Compose runs PostgreSQL 15, Redis 7 (no eviction), Gunicorn behind Nginx, a
Celery worker, and one Celery beat process. Only Nginx is published. The `init`
service collects static files, migrates, and runs the tenant ownership audit.
Volumes hold PostgreSQL, Redis, static files, the model cache, and the FAISS
index under `/var/lib/cafe`. Back up PostgreSQL and keep `.env`. Changing
`SECRET_KEY` invalidates commerce adapter credentials.

`/health` checks PostgreSQL and Redis only. Leave `MONGO_DB_URL` empty.

## HTTPS

Place certificates at
`${LETSENCRYPT_DIR}/live/${TLS_CERT_NAME}/fullchain.pem` and `privkey.pem`
before starting the TLS overlay. This repository does not issue certificates.

```dotenv
PUBLIC_URL=https://cafe.example.org
ALLOWED_HOSTS=cafe.example.org
NGINX_SERVER_NAME=cafe.example.org
TLS_CERT_NAME=cafe.example.org
HTTP_BIND=0.0.0.0
HTTP_PORT=80
LETSENCRYPT_DIR=/etc/letsencrypt
CERTBOT_DIR=/var/www/certbot
```

`PUBLIC_URL` is an origin only. `ALLOWED_HOSTS` is a comma-separated list of
hostnames, without schemes or ports.

```sh
docker compose -f docker-compose.yml -f docker-compose.tls.yml up --build -d
```

Optional SMTP: set `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`,
`EMAIL_HOST`, credentials, and `SIGNUP_ALERT_EMAIL`.

## Upgrade

Preserve the Compose project name and Postgres credentials. A new project name
creates a separate volume.

```sh
docker compose -f docker-compose.yml -f docker-compose.tls.yml stop web celery_worker celery_beat
docker compose -f docker-compose.yml -f docker-compose.tls.yml build
docker compose -f docker-compose.yml -f docker-compose.tls.yml run --rm init
bash scripts/start_production.sh
```

`scripts/start_production.sh` runs `audit_tenant_ownership --fail` in the web
image, then starts the TLS stack. It does not migrate. A direct `compose up`
skips the audit.

## Tenants

Create a superuser in the web container. Superusers use `/admin/`,
`/accounts/master-dashboard/`, and `/accounts/tenants/`. Register owners at
`/accounts/signup/` or **Tenants → Create Tenant**, then approve them. Pending,
rejected, suspended, and inactive tenants cannot use channel APIs, including
tokens issued before they were disabled. Do not run `seed_cafe_demo` for a real
café.

Dashboards, chat, voice, message workers, and order services each check tenant,
customer, and session ownership. Analytics queries are limited to that tenant
and mask order metadata and chat state, including checkout contact details,
before projection and aliases. Those safety checks cannot be disabled by a model
proposal. Django admin is limited to operations users. `scripts/start_production.sh`
does not verify native provider connectivity. Test each external adapter before
it takes live orders.

The owner enters a menu, or an external menu source
([menu adapter](../commerce/menu_adapter.md)), then publishes Knowledge
([runtime configuration](../chatbot/runtime_configuration.md)). Save **Checkout
settings** before enabling checkout topics.

## Channels

- **Website:** `/chat-page/?tenant=<slug>`. `GET /agent_core/token/?tenant=<slug>`
  with `X-API-KEY` issues a JWT; `POST /agent_core/chatbot-api/` sends a turn.
  Set `allowed_domains` for other origins. Same-origin is the tested path.
- **Telegram:** save the bot token in owner Settings after `PUBLIC_URL` is
  public HTTPS. Saving registers
  `${PUBLIC_URL}/agent_core/telegram-webhook/?token=<bot-token>`. Celery must
  be running. Local HTTP does not call Telegram.
- **WhatsApp:** signed inbound text using `WHATSAPP_APP_SECRET` and the tenant
  `whatsapp_id`. There is no GET verification handshake and no outbound sender
  in this repository.
- **Voice:** OpenAI audio and FFmpeg. The no-key smoke check does not cover it.

## Commerce

Enable commerce only after reading the [limits and protocol](../commerce/integration.md)
and the [recovery guide](../commerce/operations.md). Schedule
`reconcile_commerce` through Celery beat, or run
`python manage.py reconcile_commerce` every minute.
