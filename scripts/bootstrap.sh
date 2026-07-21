#!/bin/sh
set -eu

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env with development-only values. Replace them before production use."
fi

./scripts/validate-config.sh .env
docker compose config --quiet
docker compose up -d --build postgres redis minio minio-bootstrap backend worker scheduler superset web
docker compose run --rm backend /app/infra/docker/backend-entrypoint.sh seed
./scripts/smoke-test.sh
