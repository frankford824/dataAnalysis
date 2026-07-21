# Superset operator integration

The Compose service performs `superset db upgrade`, idempotently creates the implementation administrator, runs `superset init`, and registers **Certified Commerce Data** on every start. The connection searches only the PostgreSQL `certified` schema and cannot modify data.

The backend's BI adapter owns dataset and dashboard synchronization after a semantic-model version passes quality gates. The semantic model remains the sole metric-definition source; chart designers must not redefine certified amount formulas inside Superset. Optional exported dashboard archives placed in `infra/superset/assets/*.zip` are imported at boot, but customer exports should live in protected deployment storage rather than Git.

Business users open dashboards only through the product portal. The portal uses `PUBLIC_SUPERSET_URL` as the browser-visible Superset origin and sends a five-minute guest token whose RLS clause is fixed to the current enterprise. Implementation staff use the **Advanced report design** link with an administrator account. The embedded viewer role starts with no broad datasource, database, or schema grant.

For production, set a portal-origin CSP at the reverse proxy and restrict `/superset/login` to the implementation network. Rotate Superset secrets only during a planned maintenance window because existing sessions/guest tokens become invalid.
