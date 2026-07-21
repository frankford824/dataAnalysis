# Security and integration boundaries

## Tenant and publication boundary

All formal domain records carry `enterprise_id`; API authorization and tests must reject cross-enterprise identifiers even if a user guesses a UUID. Raw objects are stored under enterprise-scoped keys and retrieved through short-lived URLs after authorization. Draft rules have no direct path to certified data. Amounts, joins, deduplication, allocation, and aggregation are deterministic; AI output remains a draft until reviewed, compiled, regression-tested, and version-published.

## Superset

Superset connects as `analytics_reader`, which can only `SELECT` from the `certified` schema, is forced into read-only transactions, and has a 60-second statement timeout. It cannot see raw/staging tables or write back. `register_analytics.py` disables SQL Lab exposure, DML, CTAS/CVAS, and file upload on the certified connection. Implementation staff use the separate Superset admin account through the internal design route.

The backend publishes enterprise-specific datasets and grants embedded dashboard access after its own RBAC check. It mints five-minute guest tokens server-side. Never expose `SUPERSET_SECRET_KEY`, `SUPERSET_GUEST_TOKEN_SECRET`, database credentials, or admin credentials to the browser. Production reverse proxies must add TLS, clickjacking/CSP policy compatible with the single approved portal origin, and access logs.

## AI

LiteLLM is an optional profile and has no enabled provider model in source control. Provider keys are encrypted at rest by the backend and supplied to the private runtime configuration. Cloud tasks receive only filenames, headers, field profiles, and a small masked sample by default. Natural-language SQL is limited to certified views, one read-only statement, a row cap, and a timeout, with prompt/model/query/audit metadata retained. Provider failure cannot open a deterministic publication gate.

## Object and secret handling

MinIO buckets prohibit anonymous access; raw inputs have versioning and object lock. Original object keys are content-addressed and never overwritten. `.env`, backups, uploaded files, PBIX files, and generated exports are ignored by Git. Rotate all development defaults before production; `validate-config.sh` rejects them in `APP_ENV=production`.
