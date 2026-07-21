#!/bin/sh
set -eu

bundle_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$bundle_dir"
sha256sum -c SHA256SUMS
docker image load -i images.tar.gz
mkdir -p application
tar -xzf application.tar.gz -C application
echo "Images loaded. Copy env.template to application/.env, set production secrets, then run:"
echo "  cd application && ./scripts/validate-config.sh && docker compose up -d"
