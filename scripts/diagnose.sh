#!/bin/sh
set -eu

echo "== compose services =="
docker compose ps
echo "== compose configuration =="
docker compose config --quiet && echo "valid"
echo "== storage =="
docker system df
echo "== recent unhealthy logs =="
for service in postgres redis minio backend worker scheduler web superset; do
  container_id="$(docker compose ps -q "$service" 2>/dev/null || true)"
  [ -n "$container_id" ] || continue
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id")"
  printf '%s: %s\n' "$service" "$health"
  if [ "$health" != healthy ] && [ "$health" != running ]; then
    docker compose logs --tail=50 "$service"
  fi
done
