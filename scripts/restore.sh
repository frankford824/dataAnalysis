#!/bin/sh
set -eu

if [ "${1:-}" != "--confirm" ] || [ -z "${2:-}" ]; then
  echo "usage: $0 --confirm /absolute/path/to/backup" >&2
  echo "restore replaces the current databases and object buckets" >&2
  exit 64
fi

source_dir="$(cd "$2" && pwd)"
[ -f "$source_dir/SHA256SUMS" ] || { echo "missing SHA256SUMS" >&2; exit 1; }
(cd "$source_dir" && sha256sum -c SHA256SUMS)
[ ! -f .env ] || { set -a; . ./.env; set +a; }

docker compose stop backend worker scheduler web superset
docker compose cp "$source_dir/postgres/commerce.dump" postgres:/tmp/commerce.dump
docker compose cp "$source_dir/postgres/superset.dump" postgres:/tmp/superset.dump
docker compose cp "$source_dir/postgres/litellm.dump" postgres:/tmp/litellm.dump
docker compose exec -T postgres pg_restore \
  --username "${POSTGRES_USER:-commerce}" --dbname "${POSTGRES_DB:-commerce}" \
  --clean --if-exists --no-owner /tmp/commerce.dump
docker compose exec -T postgres pg_restore \
  --username "${POSTGRES_USER:-commerce}" --dbname superset \
  --clean --if-exists --no-owner /tmp/superset.dump
docker compose exec -T postgres pg_restore \
  --username "${POSTGRES_USER:-commerce}" --dbname litellm \
  --clean --if-exists --no-owner /tmp/litellm.dump

network="${COMPOSE_PROJECT_NAME:-commerce-analytics}_internal"
docker run --rm --network "$network" \
  -e MINIO_ROOT_USER="${MINIO_ROOT_USER:-minioadmin}" \
  -e MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-minioadmin-dev-only}" \
  -e S3_RAW_BUCKET="${S3_RAW_BUCKET:-commerce-raw}" \
  -e S3_INTERMEDIATE_BUCKET="${S3_INTERMEDIATE_BUCKET:-commerce-intermediate}" \
  -e S3_EXPORT_BUCKET="${S3_EXPORT_BUCKET:-commerce-exports}" \
  -v "$source_dir/objects:/backup:ro" \
  --entrypoint /bin/sh minio/mc:RELEASE.2024-07-15T17-46-06Z -ec '
    mc alias set target http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
    for bucket in "$S3_RAW_BUCKET" "$S3_INTERMEDIATE_BUCKET" "$S3_EXPORT_BUCKET"; do
      mc mb --ignore-existing "target/$bucket"
      mc mirror --overwrite --remove "/backup/$bucket" "target/$bucket"
    done
  '

docker compose up -d backend worker scheduler web superset
./scripts/smoke-test.sh
echo "restore completed from $source_dir"
