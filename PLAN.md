# Commerce Analytics Platform delivery plan

1. Establish the monorepo contract: FastAPI/PostgreSQL/Redis/MinIO processing core, React web and shared Tauri desktop client, Superset BI adapter, and LiteLLM gateway with an explicit no-AI mode.
2. Build the enterprise-scoped domain and versioned configuration APIs, deterministic ingestion pipeline, certified-query boundary, RBAC/audit controls, migrations, seed tenants, and acceptance tests.
3. Build the three-step business experience plus administration and embedded analytics surfaces, keeping operational language business-facing and sharing upload behavior with the desktop shell.
4. Wire a reproducible Docker stack with health checks, setup/backup/restore/SBOM utilities, capacity and offline deployment guidance, then verify tests, builds, migrations, containers, tenant isolation, and end-to-end upload/publish/query behavior.
5. Review only task-owned changes, commit on `main`, and push the verified result to `origin/main`.

## Delivery boundary

The repository ships a production-shaped single-customer/single-instance baseline with tenant keys and isolation tests. External Power BI Desktop automation, live third-party AI calls, and customer-specific Superset SSO require the customer's licensed software or credentials; the shipped adapters must degrade safely without them.
