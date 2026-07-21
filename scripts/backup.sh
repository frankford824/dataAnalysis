#!/bin/sh
set -eu

umask 077
[ ! -f .env ] || { set -a; . ./.env; set +a; }
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
destination="${1:-backups/$timestamp}"
mkdir -p "$destination/postgres" "$destination/objects" "$destination/config"
destination="$(cd "$destination" && pwd)"

if [ -f .env ]; then
  cp .env "$destination/config/runtime.env"
fi
cp compose.yaml "$destination/config/compose.yaml"
cp -R docs/config "$destination/config/templates"

docker compose exec -T postgres pg_dump \
  --username "${POSTGRES_USER:-commerce}" \
  --dbname "${POSTGRES_DB:-commerce}" \
  --format=custom > "$destination/postgres/commerce.dump"
docker compose exec -T postgres pg_dump \
  --username "${POSTGRES_USER:-commerce}" \
  --dbname superset \
  --format=custom > "$destination/postgres/superset.dump"

network="${COMPOSE_PROJECT_NAME:-commerce-analytics}_internal"
docker run --rm --network "$network" \
  -e MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}" \
  -e MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin-dev-only}" \
  -e S3_RAW_BUCKET="${S3_RAW_BUCKET:-commerce-raw}" \
  -e S3_INTERMEDIATE_BUCKET="${S3_INTERMEDIATE_BUCKET:-commerce-intermediate}" \
  -e S3_EXPORT_BUCKET="${S3_EXPORT_BUCKET:-commerce-exports}" \
  -v "$destination/objects:/backup" \
  --entrypoint /bin/sh minio/mc:RELEASE.2024-07-15T17-46-06Z -ec '
    mc alias set source http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
    for bucket in "$S3_RAW_BUCKET" "$S3_INTERMEDIATE_BUCKET" "$S3_EXPORT_BUCKET"; do
      mkdir -p "/backup/$bucket"
      mc mirror --overwrite "source/$bucket" "/backup/$bucket"
    done
  '

(cd "$destination" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS)
echo "backup completed: $destination"
echo "Protect this directory: it contains database content, original files, and runtime secrets."
