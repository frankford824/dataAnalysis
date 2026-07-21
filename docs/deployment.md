# Deployment configuration guide

## Supported topology

Each customer runs one isolated instance on a customer-managed Linux server or VM. The application still scopes every formal record by `enterprise_id`; a single instance may therefore host several approved enterprises without weakening the future tenant boundary. Windows and macOS are clients, not server platforms.

Required server software is Docker Engine 26+ with Compose v2.27+. Production requires internal DNS, a TLS reverse proxy, an SMTP relay if alert email is enabled, and storage included in the customer's backup policy. PostgreSQL, Redis, MinIO, the API, workers, scheduler, web portal, and Superset run on the server. LiteLLM is optional.

## First installation

```bash
./scripts/generate-env.sh > .env
chmod 600 .env
# Edit the administrator email and customer-specific network ports.
./scripts/validate-config.sh .env
docker compose config --quiet
docker compose up -d --build postgres redis minio minio-bootstrap backend worker scheduler superset web
docker compose run --rm backend /app/infra/docker/backend-entrypoint.sh seed
./scripts/smoke-test.sh
```

Do not publish PostgreSQL, Redis, MinIO, API, or Superset directly to the internet. In production, bind their host ports to a management interface or remove the `ports` entries with a private Compose overlay. Only the TLS reverse proxy should expose the portal. Store `.env` in the customer's secret vault and never in Git.

The normal stack deliberately omits LiteLLM. Core ingestion, deterministic calculations, certified views, exports, and dashboards work with `AI_MODE=disabled`. To enable a reviewed AI configuration, set `AI_MODE=local` or `cloud`, add an uncommitted LiteLLM overlay and start `docker compose --profile ai up -d litellm`.

## Activation and history boundary

Set `activation_at` for each enterprise/store/source before accepting production files. Files whose content period predates that boundary remain outside the formal flow. Historical backfill is created as a separate run and may not replace a locked accounting period. Folder names are not used as a period source.

## TLS and identity

Terminate TLS at a customer-managed reverse proxy, forward `X-Forwarded-Proto`, and integrate the backend with the customer's OIDC/SAML provider when available. Superset admin credentials are for implementation staff only. Business users receive short-lived embedded guest tokens from the backend after platform RBAC checks; the browser never receives database or Superset admin credentials.

## Storage paths

Named volumes contain all mutable server data: `postgres-data`, `redis-data`, `minio-data`, and `superset-home`. Raw objects are versioned and object-locked. PostgreSQL is the source of truth for configuration, semantic versions, audit history, and object references. Take a backup before every upgrade and test restore on a separate instance at least quarterly.

See [capacity planning](capacity.md), [operations](operations.md), [offline installation](offline-deployment.md), and [security boundaries](security.md).
