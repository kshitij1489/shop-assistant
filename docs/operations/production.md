# Production

Compose runs PostgreSQL 15, Redis 7 (no eviction), Gunicorn behind Nginx, a
Celery worker, and one Celery beat process. Only Nginx is published. The `init`
service collects static files, migrates, and runs the tenant ownership audit.
Volumes hold PostgreSQL, Redis, static files, the model cache, and the FAISS
index under `/var/lib/cafe`. Back up PostgreSQL and keep the selected environment
file. Changing `SECRET_KEY` invalidates commerce adapter credentials.

`/health` checks PostgreSQL and Redis only. Leave `MONGO_DB_URL` empty.

## Guided deployment

Use a server with Docker Compose v2, Python 3.10+, and a domain pointing to it.
Allow inbound ports 80 and 443. Install a valid certificate and its private key
at `${LETSENCRYPT_DIR}/live/${TLS_CERT_NAME}/fullchain.pem` and `privkey.pem`.
The launcher uses your existing certificates; it does not issue or renew them.
Keep certificate renewal configured on the server.

From the checkout on that server:

```sh
python3 scripts/setup.py production
```

On first use, the launcher asks for the OpenAI API key, domain, and certificate
root directory. It generates independent application/database secrets and writes
`.env.production` with private permissions. It validates HTTPS settings and
certificate files before changing services. If certificates or settings are not
ready, fix them and rerun; the generated configuration is preserved.

The deployment builds the image, starts PostgreSQL and Redis, stops application
traffic/workers for migrations, collects static files, runs the ownership audit,
and starts the HTTPS stack through `scripts/start_production.sh`. Upgrades have
downtime during this sequence. No fictional café, mock services, or evaluation
controls are enabled.

Defaults are Compose project `shop-assistant-production` and image
`shop-assistant-production:local`. The local demo uses a different project,
image, environment file, and volumes. **This does not migrate an existing
installation's data:** to operate an existing deployment, pass its original
`--env-file` and `--project-name` explicitly.

You can prepare settings without starting services:

```sh
python3 scripts/setup.py production --configure-only
```

Review `.env.production` before deployment. A typical domain configuration is:

```dotenv
PUBLIC_URL=https://cafe.example.org
ALLOWED_HOSTS=cafe.example.org
NGINX_SERVER_NAME=cafe.example.org
TLS_CERT_NAME=cafe.example.org
HTTP_BIND=0.0.0.0
HTTP_PORT=80
LETSENCRYPT_DIR=/etc/letsencrypt
CERTBOT_DIR=/var/www/certbot
DEBUG=false
```

`PUBLIC_URL` is an HTTPS origin on standard port 443. `ALLOWED_HOSTS` is a
comma-separated list of hostnames. Keep `OPENAI_API_KEY` set for live chat and
choose models available to that key. Optional SMTP: set
`EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`, `EMAIL_HOST`,
credentials, and `SIGNUP_ALERT_EMAIL`.

## Operator account

After successful deployment the launcher prints an account-creation command.
For the default project, it is equivalent to:

```sh
export APP_ENV_FILE="$PWD/.env.production"
export COMPOSE_PROJECT_NAME=shop-assistant-production
export APP_IMAGE=shop-assistant-production:local
docker compose --env-file "$APP_ENV_FILE" -f docker-compose.yml -f docker-compose.tls.yml exec web python manage.py createsuperuser
```

Open `https://YOUR_DOMAIN/accounts/login/`, then create your restaurant, publish
its knowledge/menu, and configure its channels. Configure and test external
commerce adapters before accepting live orders.

## Upgrade and recovery

Back up PostgreSQL and `.env.production` first. Keep the same Compose project
name, secrets, and database credentials, then rerun:

```sh
python3 scripts/setup.py production
```

Existing environment files are never overwritten. Changing the project name
selects a new set of volumes. A migration or ownership audit failure stops the
launcher before application startup. Review the error, repair the failure, and
rerun. The launcher does not roll back migrations or remove volumes.

For an older deployment, preserve its original project and environment file:

```sh
python3 scripts/setup.py production --env-file .env --project-name YOUR_EXISTING_PROJECT
```

For manual operations, export the three variables in **Operator account** for
the intended deployment before invoking Compose or `scripts/start_production.sh`.
The latter remains an audit-and-start gate; it does not build or migrate by
itself. The guided launcher performs those steps first.

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
