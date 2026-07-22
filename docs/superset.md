# Superset operator integration

The Compose service performs `superset db upgrade`, idempotently creates the implementation administrator, runs `superset init`, and registers **Certified Commerce Data** on every start. The connection searches only the PostgreSQL `certified` schema and cannot modify data.

Startup registers a physical `certified.sales` dataset, semantic metrics, a starter table chart, and the embedded `commerce-overview` dashboard. The PostgreSQL view contains only platform-published results. The platform semantic model remains the metric-definition source; chart designers must not redefine certified amount formulas inside Superset. Optional exported dashboard archives placed in `infra/superset/assets/*.zip` are imported at boot, but customer exports should live in protected deployment storage rather than Git.

Business users open dashboards only through the product portal. The portal uses `PUBLIC_SUPERSET_URL` as the browser-visible Superset origin and sends a five-minute guest token. Every token must contain an RLS clause for exactly one `enterprise_id`, with an optional allow-list of `store_id` values. `scripts/superset-guest-token.py` is a demo/verification implementation with UUID validation and a maximum ten-minute lifetime; production tokens come only from the backend after its RBAC check. Implementation staff use the **Advanced report design** link with an administrator account. The embedded viewer role receives explicit access to `certified.sales`, not a database-wide or schema-wide grant.

For production, set a portal-origin CSP at the reverse proxy and restrict `/superset/login` to the implementation network. Rotate Superset secrets only during a planned maintenance window because existing sessions/guest tokens become invalid.
