#!/bin/sh
set -eu

if [ "${1:-}" != "--confirm" ]; then
  echo "usage: $0 --confirm" >&2
  echo "This deletes only the current Compose project's named volumes, then recreates the demo." >&2
  exit 64
fi

project="${COMPOSE_PROJECT_NAME:-commerce-analytics}"
case "$project" in
  ""|/|.|..) echo "unsafe Compose project name" >&2; exit 1 ;;
esac

docker compose down --volumes --remove-orphans
export DEMO_AUTO_SETUP=true
export DEMO_ENTERPRISE_NAME="${DEMO_ENTERPRISE_NAME:-Example Outdoor Retail}"
export DEMO_ADMIN_NAME="${DEMO_ADMIN_NAME:-Example Platform Administrator}"
export DEMO_ADMIN_EMAIL="${DEMO_ADMIN_EMAIL:-demo-admin@example.invalid}"
export DEMO_ADMIN_PASSWORD="${DEMO_ADMIN_PASSWORD:-development-demo-only}"
export SMOKE_ADMIN_EMAIL="$DEMO_ADMIN_EMAIL"
export SMOKE_ADMIN_PASSWORD="$DEMO_ADMIN_PASSWORD"
./scripts/bootstrap.sh
./scripts/demo-seed.sh
echo "repeatable demo reset completed for Compose project: $project"
