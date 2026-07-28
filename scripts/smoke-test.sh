#!/bin/sh
set -eu

[ ! -f .env ] || { set -a; . ./.env; set +a; }
backend_url="${BACKEND_URL:-http://localhost:${BACKEND_PORT:-8000}}"
web_url="${WEB_URL:-http://localhost:${WEB_PORT:-3000}}"
superset_url="${SUPERSET_URL:-http://localhost:${SUPERSET_PORT:-8088}}"

wait_http() {
  name="$1"
  url="$2"
  attempts="${3:-60}"
  i=0
  until curl -fsS "$url" >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge "$attempts" ]; then
      echo "$name did not become healthy at $url" >&2
      docker compose ps >&2 || true
      exit 1
    fi
    sleep 2
  done
  echo "$name healthy: $url"
}

wait_http backend "$backend_url/ready"
wait_http web "$web_url/healthz"
wait_http superset "$superset_url/health"

docker compose exec -T postgres psql -U "${POSTGRES_USER:-commerce}" -d "${POSTGRES_DB:-commerce}" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
SELECT 1;
SELECT rolname FROM pg_roles WHERE rolname = 'analytics_reader';
SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'certified';
SQL

docker compose exec -T redis redis-cli ping | grep -q PONG
curl -fsS "http://localhost:${MINIO_API_PORT:-9000}/minio/health/ready" >/dev/null

setup_status="$(curl -fsS "$backend_url/api/v1/setup/status")"
initialized="$(printf '%s' "$setup_status" | python3 -c 'import json,sys; print("true" if json.load(sys.stdin).get("initialized") else "false")')"
cookie_jar="$(mktemp)"
trap 'rm -f "$cookie_jar"' EXIT
authenticated=false
if [ -n "${SMOKE_ADMIN_EMAIL:-}" ] && [ -n "${SMOKE_ADMIN_PASSWORD:-}" ]; then
  login_payload="$(python3 - <<'PY'
import json, os
print(json.dumps({"email": os.environ["SMOKE_ADMIN_EMAIL"], "password": os.environ["SMOKE_ADMIN_PASSWORD"]}))
PY
)"
  curl -fsS -c "$cookie_jar" -H 'Content-Type: application/json' --data "$login_payload" "$backend_url/api/v1/auth/login" >/dev/null
  authenticated=true
fi
if [ "$authenticated" = true ]; then
  diagnostics="$(curl -fsS -b "$cookie_jar" "$backend_url/api/v1/health/diagnostics")"
  printf '%s' "$diagnostics" | python3 -c '
import json, sys
result = json.load(sys.stdin)
if result.get("status") != "healthy":
    raise SystemExit(f"backend dependency diagnostics are not healthy: {result}")
'
elif [ "$initialized" = false ]; then
  echo "authenticated diagnostics skipped: browser first-run setup is pending"
elif [ "${ALLOW_AUTH_SKIP:-false}" = true ]; then
  echo "authenticated diagnostics skipped explicitly; supply SMOKE_ADMIN_EMAIL and SMOKE_ADMIN_PASSWORD for a full check"
else
  echo "initialized service requires SMOKE_ADMIN_EMAIL and SMOKE_ADMIN_PASSWORD" >&2
  exit 1
fi

wait_service_health() {
  service="$1"
  container_id="$(docker compose ps -q "$service")"
  [ -n "$container_id" ] || { echo "required service is missing: $service" >&2; exit 1; }
  attempts=0
  while :; do
    state="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
    case "$state" in
      healthy|running) return 0 ;;
      starting)
        attempts=$((attempts + 1))
        [ "$attempts" -lt 60 ] || { echo "$service remained in state $state" >&2; exit 1; }
        sleep 2
        ;;
      *) echo "$service state is $state" >&2; exit 1 ;;
    esac
  done
}

for service in postgres redis minio backend worker scheduler web superset; do
  wait_service_health "$service"
done

if docker compose ps --status running --services | grep -qx litellm; then
  echo "LiteLLM must not run in the default no-AI stack" >&2
  exit 1
fi

echo "core stack smoke test passed (AI is optional and was not required)"
