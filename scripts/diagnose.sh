#!/bin/sh
set -eu

[ ! -f .env ] || { set -a; . ./.env; set +a; }
backend_url="${BACKEND_URL:-http://127.0.0.1:${BACKEND_PORT:-8000}}"
web_url="${WEB_URL:-http://127.0.0.1:${WEB_PORT:-3000}}"
superset_url="${SUPERSET_URL:-http://127.0.0.1:${SUPERSET_PORT:-8088}}"
minio_url="${MINIO_URL:-http://127.0.0.1:${MINIO_API_PORT:-9000}}"

echo "== compose services =="
docker compose ps
echo "== compose configuration =="
docker compose config --quiet && echo "valid"
echo "== storage =="
docker system df
echo "== recent unhealthy logs =="
failed=0
for service in postgres redis minio backend worker scheduler web superset; do
  container_id="$(docker compose ps -q "$service" 2>/dev/null || true)"
  if [ -z "$container_id" ]; then
    echo "$service: missing" >&2
    failed=1
    continue
  fi
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
  printf '%s: %s\n' "$service" "$health"
  if [ "$health" != healthy ] && [ "$health" != running ]; then
    failed=1
    docker compose logs --tail=50 "$service"
  fi
done

if ! curl -fsS "$backend_url/ready" >/dev/null; then
  echo "backend dependency readiness failed" >&2
  failed=1
fi
if ! curl -fsS "$superset_url/health" >/dev/null; then
  echo "Superset functional health failed" >&2
  failed=1
fi
if ! curl -fsS "$web_url/healthz" >/dev/null; then
  echo "web functional health failed" >&2
  failed=1
fi
if ! curl -fsS "$minio_url/minio/health/ready" >/dev/null; then
  echo "MinIO functional health failed" >&2
  failed=1
fi
if ! docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
  echo "Redis functional health failed" >&2
  failed=1
fi
if ! docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-commerce}" -d "${POSTGRES_DB:-commerce}" >/dev/null; then
  echo "PostgreSQL functional health failed" >&2
  failed=1
fi

[ "$failed" -eq 0 ] || exit 1
echo "all required services are healthy"
