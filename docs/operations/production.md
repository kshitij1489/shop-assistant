# Production

Run these steps on the VPS with a local Docker Engine. Commands below assume a
root shell, which can read the certificate private key and access Docker.
Use **production** mode: `python3 scripts/setup.py` without a mode starts the
local demo on port 8080.

The examples use `www.example.com`. Replace it with the hostname you own, such
as `chat-wise.ai`. Use the same hostname for DNS, configuration and certificates.

## First deployment

### 1. Install prerequisites

Install [Docker Engine and Compose v2](https://docs.docker.com/engine/install/ubuntu/).
On Ubuntu 24.04 or newer, install the remaining host tools:

```sh
apt update
apt install -y git python3 curl openssl certbot
docker compose version
docker info
```

Python 3.10+, OpenSSL 1.1.1+ and curl are needed by the production launcher.
The renewal instructions use Certbot 2.3+ (`certbot --version`). For other systems,
follow the [Certbot installation instructions](https://certbot.eff.org/instructions).

Clone the repository, or enter your existing checkout:

```sh
git clone https://github.com/kshitij1489/shop-assistant.git
cd shop-assistant
DOMAIN=www.example.com
```

### 2. Point DNS to the VPS

<a id="dns"></a>

At the provider managing your domain's nameservers, set an **A record** for your
chosen hostname to the VPS's public IPv4 address. Use `@` for an apex domain or
`www` for the `www` subdomain. Remove conflicting records for that hostname.
Only keep an AAAA record if IPv6 is also configured on this deployment.

Allow inbound TCP **80 and 443** in the provider firewall and host firewall,
while keeping SSH access. Check DNS from the VPS:

```sh
getent ahostsv4 "$DOMAIN"
```

The result should include your VPS address (or your intentionally configured
external proxy). The launcher prints DNS results, but cannot determine your
VPS's public IP or prove that an external firewall permits browser traffic.

### 3. Check for existing web servers

<a id="port-conflicts"></a>

```sh
ss -ltnp '( sport = :80 or sport = :443 )'
docker ps --format 'table {{.Names}}\t{{.Ports}}'
```

A first deployment needs these ports available. If host Nginx belongs only to
retired sites, stop it and prevent it from restarting on reboot:

```sh
systemctl disable --now nginx
```

For obsolete Docker services, use their actual names:

```sh
docker update --restart=no OLD_PROXY_CONTAINER OLD_APP_CONTAINER
docker stop OLD_PROXY_CONTAINER OLD_APP_CONTAINER
```

If these services host active sites, keep them running and arrange routing
through that existing proxy. The container can use loopback ports via
`HTTP_BIND=127.0.0.1`, `HTTP_PORT=18080`, `HTTPS_BIND=127.0.0.1` and
`HTTPS_PORT=18443`. The host proxy must route your domain to
`https://127.0.0.1:18443` with the correct Host header and TLS server name, and
handle public TLS and ACME challenges. This is a separate proxy configuration;
changing port numbers alone does not create it. `PUBLIC_URL` remains standard
public HTTPS. [Nginx proxy configuration](https://nginx.org/en/docs/http/ngx_http_proxy_module.html).

On upgrades, the launcher recognizes unchanged port and bind-address mappings
owned by the same Compose project and service. If you change a bind address on
an occupied port, preflight prints a command to stop only that service. Run it,
then rerun setup so it can check the new address before deployment.

### 4. Generate configuration

```sh
python3 scripts/setup.py production --configure-only
```

Enter your OpenAI API key, the hostname without `https://`, and the certificate
root (normally `/etc/letsencrypt`). The launcher creates `.env.production` with
private permissions and independent application/database secrets. Existing files
are preserved; edit one with `nano .env.production` when needed.

For a direct deployment, these settings should match your hostname:

```dotenv
PUBLIC_URL=https://www.example.com
ALLOWED_HOSTS=www.example.com
NGINX_SERVER_NAME=www.example.com
TLS_CERT_NAME=www.example.com
HTTP_BIND=0.0.0.0
HTTP_PORT=80
HTTPS_BIND=0.0.0.0
HTTPS_PORT=443
LETSENCRYPT_DIR=/etc/letsencrypt
CERTBOT_DIR=/var/www/certbot
DEBUG=false
```

`CSRF_TRUSTED_ORIGINS` defaults to `PUBLIC_URL`. Keep `OPENAI_API_KEY` set for live
chat and choose models available to that key. Internal Python/database names
are not domain settings; preserve existing database credentials and secrets.

### 5. Create the HTTPS certificate

<a id="certificates"></a>

The launcher checks certificates but does **not** issue or renew them. For a new
server with port 80 free and DNS ready, set the shell variable to the same
`LETSENCRYPT_DIR` value as your environment file:

```sh
LETSENCRYPT_DIR=/etc/letsencrypt
certbot certonly --config-dir "$LETSENCRYPT_DIR" \
  --standalone --cert-name "$DOMAIN" -d "$DOMAIN"
```

Certbot prompts for account information. It creates `fullchain.pem` and
`privkey.pem` under `$LETSENCRYPT_DIR/live/$DOMAIN/`, matching `TLS_CERT_NAME`.
Use the same `--config-dir` for every Certbot command and scheduled renewal.

If you already have certificates, inspect their names, domains and expiry:

```sh
certbot certificates --config-dir "$LETSENCRYPT_DIR"
```

An existing web server can serve Certbot's **webroot** challenge instead of
standalone; see [renewal](#renewal). A certificate file can exist but be expired,
for another hostname, or paired with the wrong key. Setup now rejects those
cases before building or stopping application services.

`example.com` and `www.example.com` are separate hostnames. To serve both, add
both DNS records, include both `-d` options when requesting the certificate, and
include both in `ALLOWED_HOSTS`. When renewing an existing certificate, retain
all hostnames you still need; review any prompt to remove names.

### 6. Check and deploy

```sh
python3 scripts/setup.py production --check
python3 scripts/setup.py production
```

`--check` reads existing configuration, inspects Docker port ownership, checks
host listeners, validates the certificate/key/hostname/dates, and resolves DNS.
It writes a log but does not build, migrate, or change services. Normal deployment
runs these checks automatically too. A port check without enough host privileges
prints a warning; rerun it as root to complete that check.

Deployment builds the image, starts PostgreSQL and Redis, stops application
traffic/workers for migrations, collects static files, runs the ownership audit,
and starts the HTTPS stack. It then verifies the configured Nginx HTTPS port
locally using the real hostname and certificate validation, followed by the public
`/health` URL. It reports success and offers administrator setup only after both pass.
An upgrade has downtime during this sequence.

The stopped Nginx container is replaced during deployment so an old, detached
network endpoint cannot survive a rerun. Its static-file volume and certificate
mount are reused.

Progress, checks and setup failures are recorded in `setup.log`; select another
path with `--log-file /path/to/setup.log`. The log excludes expanded environment
values, API keys, application logs and raw build output. Docker/build output still
appears in the terminal. Treat application logs separately when sharing diagnostics.

Defaults are project `shop-assistant-production` and image
`shop-assistant-production:local`, with separate configuration and volumes from
the demo. No demo tenant, mock services or evaluation controls are installed.

### 7. Create the operator account

After HTTPS verification, setup checks for an active administrator. If none
exists, it asks **Create your administrator account now? [Y/n]**. Press Enter
to open Django's username, email and password prompts in the same terminal.
Passwords are hidden and are not written to the setup log.

Upgrades skip this prompt when an active administrator already exists. Choosing
`n`, cancelling the wizard, using `--no-input`, or running without an interactive
terminal leaves the site running and prints the command for later. If account
creation fails, its error appears in the terminal and you can retry separately.

For manual account creation on the default project, run:

```sh
export APP_ENV_FILE="$PWD/.env.production"
export COMPOSE_PROJECT_NAME=shop-assistant-production
export APP_IMAGE=shop-assistant-production:local

docker compose --env-file "$APP_ENV_FILE" \
  -f docker-compose.yml -f docker-compose.tls.yml \
  exec web python manage.py createsuperuser
```

These exports are also used by the operations commands below; repeat them from
the checkout after reconnecting over SSH. If using a custom project, use its
original name and image. Open `https://YOUR_DOMAIN/accounts/login/`, then create
your restaurant, publish its knowledge/menu and configure channels.
`/health` checks PostgreSQL and Redis; test live chat and any commerce adapters
separately before accepting orders.

Complete the renewal configuration below before finishing the installation.

## Renewal

After the first deployment, switch Certbot from standalone to the challenge
folder already served by Docker Nginx. This keeps port 80 available during
renewal. Set `DOMAIN` to the certificate name used above, and set both directories
to their values in your environment file:

```sh
DOMAIN=www.example.com
LETSENCRYPT_DIR=/etc/letsencrypt
CERTBOT_DIR=/var/www/certbot
certbot reconfigure --config-dir "$LETSENCRYPT_DIR" --cert-name "$DOMAIN" \
  --webroot -w "$CERTBOT_DIR" \
  --deploy-hook "docker exec ${COMPOSE_PROJECT_NAME}-nginx-1 nginx -s reload"

certbot renew --config-dir "$LETSENCRYPT_DIR" --cert-name "$DOMAIN" \
  --dry-run --run-deploy-hooks
```

The deploy hook reloads the **container** after a renewal. A regular dry run
does not execute deploy hooks; `--run-deploy-hooks` also exercises the reload.
[Certbot renewal documentation](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates).

For the Ubuntu apt installation above, `certbot.timer` runs `certbot.service`.
The service does not inherit shell variables. If `LETSENCRYPT_DIR` differs from
`/etc/letsencrypt`, configure the service to use that directory:

```sh
systemctl edit certbot.service
```

Add this drop-in, replacing `/srv/letsencrypt` with your actual absolute
`LETSENCRYPT_DIR` path. The empty `ExecStart=` clears the packaged command:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/certbot -q renew --config-dir "/srv/letsencrypt"
```

This service renews certificates in the selected directory; keep separate
scheduled renewal commands for any certificates managed in other directories.
[systemd service documentation](https://manpages.debian.org/bookworm/systemd/systemd.service.5.en.html#ExecStart=).

For both the default directory and a custom directory, load the configuration,
inspect the renewal command, and enable the timer:

```sh
systemctl daemon-reload
systemctl cat certbot.service
systemctl enable --now certbot.timer
systemctl list-timers certbot.timer
```

Other Certbot installations may use a different timer or cron task. Configure
their scheduled command with the same `--config-dir` value too.

If an installed certificate is already expired while the container is running,
renew using the webroot (include any additional hostnames you still serve):

```sh
certbot certonly --config-dir "$LETSENCRYPT_DIR" --webroot -w "$CERTBOT_DIR" \
  --cert-name "$DOMAIN" -d "$DOMAIN" \
  --deploy-hook "docker exec ${COMPOSE_PROJECT_NAME}-nginx-1 nginx -s reload"
curl -f "https://$DOMAIN/health"
```

After replacing a host web server, inspect old renewal hooks:

```sh
grep -RnsE 'nginx|docker|deploy_hook|renew_hook|pre_hook|post_hook' \
  "$LETSENCRYPT_DIR/renewal-hooks" "$LETSENCRYPT_DIR/renewal" \
  /etc/letsencrypt/cli.ini
```

Remove or update only obsolete hooks. An `invalid PID` error for
`/run/nginx.pid` often means a hook tried to reload retired **host** Nginx.
Keep the container reload hook, then rerun the dry run with deploy hooks enabled.

## Troubleshooting

Run commands from the checkout with the exports in **Create the operator
account**. Start with:

```sh
docker compose --env-file "$APP_ENV_FILE" \
  -f docker-compose.yml -f docker-compose.tls.yml ps -a

docker compose --env-file "$APP_ENV_FILE" \
  -f docker-compose.yml -f docker-compose.tls.yml logs --tail 60 nginx web
```

| Symptom | Next step |
| --- | --- |
| `address already in use` | Inspect [port ownership](#port-conflicts). Retire only unused services, or configure your existing proxy. |
| `certificate has expired` | Follow [renewal](#renewal), reload container Nginx, and retry `/health`. |
| `host not found in upstream "web"` | Check whether `web` is healthy and whether both containers share a Docker network, as below. |
| Local HTTPS works, public URL fails | Compare DNS A/AAAA records with the server addresses; check host/provider firewalls and external proxy routing. |
| Compose cannot resolve the environment file | Check matching quotes, especially the API-key line. In Nano, Ctrl+E moves to the end of a long line; its closing quote may be off-screen. |
| Image pull says access denied, then build succeeds | The local app image was built successfully. Continue with the final setup result. |

To check network attachment and crashes without printing container secrets:

```sh
docker inspect --format \
  '{{.Name}} status={{.State.Status}} OOMKilled={{.State.OOMKilled}} networks={{json .NetworkSettings.Networks}}' \
  "${COMPOSE_PROJECT_NAME}-web-1" "${COMPOSE_PROJECT_NAME}-nginx-1"
```

If `web` is healthy but Nginx has `networks={}`, recreate only Nginx to restore
its Compose network and published ports:

```sh
docker compose --env-file "$APP_ENV_FILE" \
  -f docker-compose.yml -f docker-compose.tls.yml \
  up -d --no-deps --force-recreate --wait --wait-timeout 60 nginx
```

Test HTTPS locally while preserving the certificate hostname, then test DNS:

```sh
DOMAIN=www.example.com
curl -f --resolve "$DOMAIN:443:127.0.0.1" "https://$DOMAIN/health"
curl -f "https://$DOMAIN/health"
```

For a loopback proxy deployment, use its published HTTPS port (for example,
18443) in both the local URL and `--resolve` mapping. A local connection failure
is a listener/container problem to investigate before public DNS. The launcher
never disables TLS verification to make a health check pass.

## Upgrade and recovery

Back up PostgreSQL and `.env.production` first. Compose volumes also hold Redis,
static files, downloaded models and the FAISS index. Keep the same Compose
project, secrets and database credentials, then rerun:

```sh
python3 scripts/setup.py production
```

Existing environment files are preserved. Changing the project name selects a
new set of volumes; it does not migrate an old deployment. For an older setup:

```sh
python3 scripts/setup.py production --env-file .env --project-name YOUR_EXISTING_PROJECT
```

A migration/audit failure prevents application startup. An HTTPS verification
failure leaves services available for inspection; fix the cause and rerun.
Setup does not roll back migrations or delete volumes. Changing `SECRET_KEY`
invalidates commerce adapter credentials. Leave `MONGO_DB_URL` empty.

The default Celery worker runs two prefork processes. Each process loads its
embedding model on the first embedding request and reuses it for later requests.
The first request includes model loading and uses the normal task time limits.
Model loading does not run in `worker_process_init`, whose handlers must finish
within [Celery's child-startup timeout](https://docs.celeryq.dev/en/stable/userguide/signals.html#worker-process-init).
If you previously used a temporary `--pool=solo` Compose override to recover from
startup timeouts, deploy the updated image using the standard Compose files above
and omit that override. Confirm the worker stays ready and send a new chatbot
message; `/health` alone does not verify worker processing.

Optional SMTP: configure `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend`,
`EMAIL_HOST`, credentials and `SIGNUP_ALERT_EMAIL` in the environment file.

## Tenants

The administrator created by setup is a Django superuser. Superusers use `/admin/`,
`/accounts/master-dashboard/`, and `/accounts/tenants/`. Register owners at
`/accounts/signup/` or **Tenants → Create Tenant**, then approve them. Pending,
rejected, suspended, and inactive tenants cannot use channel APIs, including
tokens issued before they were disabled. Do not run `seed_cafe_demo` for a real
café.

The tenant deletion endpoint is for unused accounts. Customer, order, chat,
menu, tax and other business records block deletion; deactivate those tenants
instead. Protected provider connections, stock and accepted orders also block it.
Successful deletion removes disposable onboarding rows, linked owner accounts,
and the tenant's Redis `msgs`, `ac`, `acidx` and `acglobal` keys. Operations accounts
are protected. Success, blocked attempts, missing tenants and Redis failures log
the actor and tenant IDs without logging transcript contents.

Redis cleanup runs synchronously in a Redis transaction before SQL commit. Scan
or execution failures prevent a success message and roll SQL changes back. SQL
and Redis do not share a distributed transaction: an uncertain Redis response or
a subsequent SQL commit failure can leave Redis data removed while SQL is restored.
This endpoint does not erase existing business history.

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
  be running. Local HTTP does not call Telegram. Existing tokens are read-only;
  changing or disconnecting a bot is disabled with **Coming soon** labels.
  The saved token can still register its webhook again.
- **WhatsApp:** **Coming soon** and disabled. Valid signed inbound requests return
  `503` before customer, session or order processing. The inbound foundation uses
  `WHATSAPP_APP_SECRET` and the tenant `whatsapp_id`, but verification and outbound
  replies are unfinished. Saving the WhatsApp contact number remains available.
- **Voice:** OpenAI audio and FFmpeg. The no-key smoke check does not cover it.

## Commerce

Enable commerce only after reading the [limits and protocol](../commerce/integration.md)
and the [recovery guide](../commerce/operations.md). Schedule
`reconcile_commerce` through Celery beat, or run
`python manage.py reconcile_commerce` every minute.
