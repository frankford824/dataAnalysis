#!/bin/sh
set -eu

if [ "${1:-}" != "--confirm" ]; then
  echo "usage: $0 --confirm" >&2
  echo "This destructive rehearsal replaces the current demo volumes." >&2
  exit 64
fi

rehearsal="artifacts/restore-rehearsal/$(date -u +%Y%m%dT%H%M%SZ)"
./scripts/integration-e2e.sh
./scripts/backup.sh "$rehearsal"
docker compose down --volumes --remove-orphans
docker compose up -d postgres redis minio minio-bootstrap
./scripts/restore.sh --confirm "$rehearsal"
./scripts/integration-e2e.sh
echo "destructive backup/restore rehearsal passed: $rehearsal"
