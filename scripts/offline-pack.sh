#!/bin/sh
set -eu

destination="${1:-artifacts/offline}"
mkdir -p "$destination"
destination="$(cd "$destination" && pwd)"

docker compose build
docker compose pull --ignore-buildable

images="$(docker compose config --images | sort -u)"
# shellcheck disable=SC2086
docker image save $images | gzip -9 > "$destination/images.tar.gz"

tar \
  --exclude=.git \
  --exclude=.env \
  --exclude=artifacts \
  --exclude=backups \
  --exclude=node_modules \
  --exclude='__pycache__' \
  -czf "$destination/application.tar.gz" .

cp .env.example "$destination/env.template"
cp scripts/offline-install.sh "$destination/install.sh"
(cd "$destination" && sha256sum images.tar.gz application.tar.gz env.template install.sh > SHA256SUMS)
echo "offline bundle created at $destination"
