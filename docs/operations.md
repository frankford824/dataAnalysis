# Operations, backup, upgrade, and rollback

## Daily checks

Run `./scripts/diagnose.sh` and monitor API job failures, queue depth, PostgreSQL disk/connection utilization, MinIO capacity, certificate expiry, and the most recent successful backup. `/health` is process liveness, `/ready` verifies database and object storage, and `/api/v1/health/diagnostics` is an authenticated dependency check.

## Backup and restore

`./scripts/backup.sh /secure/backup/path` writes custom-format dumps for the application, Superset metadata, and optional LiteLLM metadata databases; every current object from the three product buckets; the Superset/runtime configuration; and a SHA-256 manifest. Raw keys are content-addressed and never overwritten, so this includes every formal original even though MinIO's internal version IDs are not preserved. It does not back up Redis because Redis contains disposable queue/cache state. Encrypt backup media and restrict it as customer data.

Restore is intentionally destructive and requires an explicit flag:

```bash
./scripts/restore.sh --confirm /secure/backup/path
```

The script verifies hashes, pauses application writers, restores PostgreSQL and current objects, restarts services, and runs the smoke test. `./scripts/backup-restore-test.sh --confirm` additionally runs the deterministic two-store reconciliation before and after a destructive rehearsal. A restore is complete only after checking configuration/rule/model/dashboard counts, sample locked periods, and several raw object hashes; the smoke test alone does not prove business-level completeness.

## Upgrade

1. Export configuration, record the current Git commit and image digests, and run a fresh backup.
2. Rehearse migration and smoke/E2E tests against a restored copy.
3. Pull the signed release, review `.env.example` changes, then run `docker compose build`.
4. Stop workers and scheduler, run the backend migration role, and start the full stack.
5. Verify health, tenant isolation, a certified query, an export, and an embedded dashboard before reopening uploads.

```bash
docker compose stop worker scheduler
docker compose run --rm backend /app/infra/docker/backend-entrypoint.sh migrate
docker compose up -d --build
./scripts/smoke-test.sh
```

## Rollback

Application-only rollback is allowed when the database migration is backward-compatible: redeploy the recorded image digests. If a migration is not backward-compatible, stop all writers, restore the pre-upgrade backup, then deploy the prior images. Never point older code at a schema that its release notes mark incompatible. Locked periods remain immutable; rollback must not republish or recalculate them.
