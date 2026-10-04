#!/usr/bin/env bash
# Run only after migrations, adapter credential provisioning.
set -euo pipefail
cd "$(dirname "$0")/.."

compose=(docker compose -f docker-compose.yml -f docker-compose.tls.yml)

# Use the deployment image and environment without starting web or workers.
# Bypass the application entrypoint so this check cannot run migrations.
"${compose[@]}" run --rm --no-deps --entrypoint python web \
  manage.py audit_tenant_ownership --fail

# The ownership audit does not verify native provider connectivity.
# Check external adapters and commerce readiness before cutover.
exec "${compose[@]}" up -d --remove-orphans "$@"
