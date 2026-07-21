#!/bin/sh
set -eu

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

wait_http backend "$backend_url/health"
wait_http web "$web_url/healthz"
wait_http superset "$superset_url/health"

docker compose exec -T postgres psql -U "${POSTGRES_USER:-commerce}" -d "${POSTGRES_DB:-commerce}" -v ON_ERROR_STOP=1 <<'SQL' >/dev/null
SELECT 1;
SELECT rolname FROM pg_roles WHERE rolname = 'analytics_reader';
SELECT schema_name FROM information_schema.schemata WHERE schema_name = 'certified';
SQL

docker compose exec -T redis redis-cli ping | grep -q PONG
curl -fsS "http://localhost:${MINIO_API_PORT:-9000}/minio/health/ready" >/dev/null

echo "core stack smoke test passed (AI is optional and was not required)"
