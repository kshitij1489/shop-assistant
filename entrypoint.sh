#!/usr/bin/env bash
set -euo pipefail

ts() { date -Is; }

echo "$(ts) ENTRYPOINT: starting entrypoint.sh"

# Simple signal forwarding for background child
_child_pid=""
_term_handler() {
  echo "$(ts) ENTRYPOINT: SIGTERM/SIGINT received, forwarding to child..."
  if [ -n "${_child_pid:-}" ]; then
    kill -TERM "${_child_pid}" 2>/dev/null || true
    wait "${_child_pid}" || true
  fi
  exit 0
}
trap _term_handler TERM INT

# helper: run a command as appuser when running as root; otherwise run directly
run_as_appuser() {
  if [ "$(id -u)" -eq 0 ]; then
    if command -v runuser >/dev/null 2>&1; then
      runuser -u appuser -- "$@"
    else
      # fallback to su -p; note: su -c receives a single string, so use "$*" here intentionally
      su -p appuser -s /bin/bash -c "$*"
    fi
  else
    "$@"
  fi
}

echo "$(ts) ENTRYPOINT: Waiting for PostgreSQL..."
for i in {1..60}; do
  if nc -z "${POSTGRES_HOST:-db}" "${POSTGRES_PORT:-5432}"; then
    break
  fi
  sleep 1
  echo "$(ts) ENTRYPOINT: Still waiting for Postgres... ($i)"
done
if ! nc -z "${POSTGRES_HOST:-db}" "${POSTGRES_PORT:-5432}"; then
  echo "$(ts) ENTRYPOINT: PostgreSQL not ready after 60s" >&2
  exit 1
fi
echo "$(ts) ENTRYPOINT: PostgreSQL is up - continuing..."

# Optional: wait for Redis (disabled by default)
if [ "${WAIT_FOR_REDIS:-false}" = "true" ]; then
  echo "$(ts) ENTRYPOINT: Waiting for Redis..."
  for i in {1..30}; do
    if nc -z redis 6379; then
      break
    fi
    sleep 1
    echo "$(ts) ENTRYPOINT: Still waiting for Redis... ($i)"
  done
  if ! nc -z redis 6379; then
    echo "$(ts) ENTRYPOINT: Redis not ready after 30s" >&2
  else
    echo "$(ts) ENTRYPOINT: Redis is up"
  fi
fi

echo "$(ts) ENTRYPOINT: Fixing permissions on /app/static..."
mkdir -p /app/static
mkdir -p /var/lib/cafe
if [ "$(id -u)" -eq 0 ]; then
  mkdir -p "${HF_HOME:-/model_cache}"
  chown appuser:appuser "${HF_HOME:-/model_cache}"
  chown -R appuser:appuser /var/lib/cafe
  chown -R appuser:appuser /app/static || true
  evidence_root="${EVALUATION_EVIDENCE_ROOT:-/var/tmp/studio-eval}"
  mkdir -p "${evidence_root}"
  chown -R appuser:appuser "${evidence_root}" || true
  echo "$(ts) ENTRYPOINT: chown applied to /app/static (running as root)"
else
  echo "$(ts) ENTRYPOINT: not running as root; skipping chown"
fi

echo "$(ts) ENTRYPOINT: RUN_ENTRYPOINT_ACTIONS=${RUN_ENTRYPOINT_ACTIONS:-<not-set>}"
echo "$(ts) ENTRYPOINT: RUN_COLLECTSTATIC=${RUN_COLLECTSTATIC:-<not-set>}"

if [ "${RUN_ENTRYPOINT_ACTIONS:-True}" = "True" ]; then
  if [ "${RUN_COLLECTSTATIC:-False}" = "True" ]; then
    echo "$(ts) ENTRYPOINT: RUN_COLLECTSTATIC=True — running collectstatic (runtime)"
    if [ "$(id -u)" -eq 0 ]; then
      run_as_appuser python manage.py collectstatic --noinput 2>&1 | sed -n '1,200p' || rc=$?; rc=${rc:-0}
    else
      python manage.py collectstatic --noinput 2>&1 | sed -n '1,200p' || rc=$?; rc=${rc:-0}
    fi
    echo "$(ts) ENTRYPOINT: collectstatic exit code: ${rc}"
    if [ "${rc}" -ne 0 ]; then
      echo "$(ts) ENTRYPOINT: collectstatic failed (rc=${rc})"
      exit "${rc}"
    else
      echo "$(ts) ENTRYPOINT: collectstatic succeeded"
    fi
  else
    echo "$(ts) ENTRYPOINT: RUN_COLLECTSTATIC is not True — skipping collectstatic"
  fi

  echo "$(ts) ENTRYPOINT: Running migrations..."
  if [ "$(id -u)" -eq 0 ]; then
    run_as_appuser python manage.py migrate --noinput
  else
    python manage.py migrate --noinput
  fi
else
  echo "$(ts) ENTRYPOINT: RUN_ENTRYPOINT_ACTIONS is not True — skipping collectstatic/migrate/superuser"
fi

# Auto-create superuser if env vars provided
if [ "${RUN_ENTRYPOINT_ACTIONS:-True}" = "True" ]; then
  if [ -n "${DJANGO_SUPERUSER_USERNAME:-}" ] && [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
    echo "$(ts) ENTRYPOINT: Checking/creating Django superuser..."
    # always run as current user (Django manages DB permissions)
    run_as_appuser python - <<'PY'
import os, django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "studio_desk.settings")
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
u = os.environ.get("DJANGO_SUPERUSER_USERNAME")
e = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
p = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
if not User.objects.filter(username=u).exists():
    print("Creating superuser:", u)
    User.objects.create_superuser(username=u, email=e, password=p)
else:
    print("Superuser already exists:", u)
PY
  else
    echo "$(ts) ENTRYPOINT: DJANGO_SUPERUSER_* not fully provided, skipping superuser creation"
  fi
fi

echo "$(ts) ENTRYPOINT: final permissions check on /app/static:"
ls -ld /app/static || true
echo "$(ts) ENTRYPOINT: sample file (if exists):"
ls -l /app/static/chatbot_core/css/main.css || true

echo "$(ts) ENTRYPOINT: Starting application..."

# Final exec: if root, drop to appuser, otherwise exec as current user.
# Use runuser when available to preserve command args.
if [ "$(id -u)" -eq 0 ]; then
  chown -R appuser:appuser /app/static || true
  echo "$(ts) ENTRYPOINT: dropping to appuser and executing final command (preserving env)"
  if command -v runuser >/dev/null 2>&1; then
    exec runuser -u appuser -- "$@"
  else
    # fallback; note: su -c interprets a single string, so join args
    exec su -p appuser -s /bin/bash -c "$*"
  fi
else
  echo "$(ts) ENTRYPOINT: executing final command as current user"
  exec "$@"
fi
