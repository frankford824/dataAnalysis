#!/bin/sh
set -eu

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env with development-only values. Replace them before production use."
fi

# An explicit process-level flag is the only way automation may opt into demo
# setup; the checked-in development template deliberately defaults to false.
demo_auto_setup_override="${DEMO_AUTO_SETUP:-}"
demo_enterprise_name_override="${DEMO_ENTERPRISE_NAME:-}"
demo_admin_name_override="${DEMO_ADMIN_NAME:-}"
demo_admin_email_override="${DEMO_ADMIN_EMAIL:-}"
demo_admin_password_override="${DEMO_ADMIN_PASSWORD:-}"
set -a
. ./.env
set +a
if [ -n "$demo_auto_setup_override" ]; then
  DEMO_AUTO_SETUP="$demo_auto_setup_override"
  export DEMO_AUTO_SETUP
fi
if [ -n "$demo_enterprise_name_override" ]; then DEMO_ENTERPRISE_NAME="$demo_enterprise_name_override"; export DEMO_ENTERPRISE_NAME; fi
if [ -n "$demo_admin_name_override" ]; then DEMO_ADMIN_NAME="$demo_admin_name_override"; export DEMO_ADMIN_NAME; fi
if [ -n "$demo_admin_email_override" ]; then DEMO_ADMIN_EMAIL="$demo_admin_email_override"; export DEMO_ADMIN_EMAIL; fi
if [ -n "$demo_admin_password_override" ]; then DEMO_ADMIN_PASSWORD="$demo_admin_password_override"; export DEMO_ADMIN_PASSWORD; fi
./scripts/validate-config.sh .env
docker compose config --quiet
docker compose up -d --build postgres redis minio minio-bootstrap backend worker scheduler superset web

attempt=0
until curl -fsS "http://127.0.0.1:${BACKEND_PORT:-8000}/health" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "backend did not become available for first-run setup" >&2
    docker compose logs --tail=100 backend >&2 || true
    exit 1
  fi
  sleep 2
done
setup_status="$(curl -fsS "http://127.0.0.1:${BACKEND_PORT:-8000}/api/v1/setup/status")"
if ! printf '%s' "$setup_status" | python3 -c 'import json,sys; raise SystemExit(0 if json.load(sys.stdin).get("initialized") else 1)'; then
  if [ "${DEMO_AUTO_SETUP:-false}" != true ]; then
    echo "Application setup is pending. Open http://127.0.0.1:${WEB_PORT:-3000}/setup to create the first enterprise and administrator."
    ALLOW_AUTH_SKIP=true ./scripts/smoke-test.sh
    exit 0
  fi
  [ "${APP_ENV:-development}" != production ] || {
    echo "DEMO_AUTO_SETUP is forbidden in production" >&2
    exit 1
  }
  setup_payload="$(python3 - <<'PY'
import json, os
print(json.dumps({
    "enterprise_name": os.environ["DEMO_ENTERPRISE_NAME"],
    "activation_at": "2026-01-01T00:00:00Z",
    "name": os.environ["DEMO_ADMIN_NAME"],
    "email": os.environ["DEMO_ADMIN_EMAIL"],
    "password": os.environ["DEMO_ADMIN_PASSWORD"],
}))
PY
)"
  curl -fsS \
    -H 'Content-Type: application/json' \
    --data "$setup_payload" \
    "http://127.0.0.1:${BACKEND_PORT:-8000}/api/v1/setup/complete" >/dev/null
  echo "DEMO_AUTO_SETUP created the disposable demonstration administrator."
fi
./scripts/smoke-test.sh
