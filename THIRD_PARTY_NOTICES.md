# Third-party notices

This repository orchestrates, but does not copy into its original source, third-party components. A release-specific transitive inventory is generated with `./scripts/generate-sbom.sh` and must be archived with each customer release. Verify the actual lockfiles and image digests before distribution.

| Component | Typical license | Role | Upstream |
|---|---|---|---|
| FastAPI | MIT | HTTP API | <https://github.com/fastapi/fastapi> |
| SQLAlchemy | MIT | persistence | <https://github.com/sqlalchemy/sqlalchemy> |
| Alembic | MIT | database migrations | <https://github.com/sqlalchemy/alembic> |
| PostgreSQL | PostgreSQL License | database | <https://www.postgresql.org/> |
| Redis | RSALv2/SSPLv1 for Redis 7.4 distribution; review commercial terms | task/cache service | <https://redis.io/legal/licenses/> |
| Celery | BSD-3-Clause | job queue and scheduler | <https://github.com/celery/celery> |
| MinIO | AGPL-3.0 server distribution | external object-storage service | <https://github.com/minio/minio> |
| Polars | MIT | deterministic tabular processing | <https://github.com/pola-rs/polars> |
| DuckDB | MIT | deterministic analytical processing | <https://github.com/duckdb/duckdb> |
| React | MIT | web UI | <https://github.com/facebook/react> |
| Vite | MIT | web build tooling | <https://github.com/vitejs/vite> |
| Tauri | Apache-2.0 / MIT | desktop shell | <https://github.com/tauri-apps/tauri> |
| Apache Superset | Apache-2.0 | external BI service | <https://github.com/apache/superset> |
| Superset Embedded SDK | Apache-2.0 | guest-token dashboard embedding | <https://github.com/apache/superset/tree/master/superset-embedded-sdk> |
| LiteLLM | MIT for open-source proxy; verify selected edition | optional external AI gateway | <https://github.com/BerriAI/litellm> |
| nginx | BSD-2-Clause | web/reverse proxy | <https://nginx.org/> |

AGPL/SSPL/RSAL components are deployed as separately communicating services and are not linked or incorporated into the original platform source. This is a technical separation statement, not legal advice. Distribution and hosted-service obligations must be reviewed by the seller's counsel for the exact versions and commercial model. Customer-provided Power BI/PBIX functionality is governed by Microsoft's license and is not redistributed here.
