# Deterministic two-store demo

This data set represents June 2026 activity for two stores in one enterprise. `orders.csv.template` contains six sales/refund rows; `fees.csv.template` contains explicit platform, advertising, and shipping charges. The seed script replaces `STORE_ALPHA` and `STORE_BETA` with actual generated store IDs before upload.

`expected-reconciliation.csv` is the independent manual control total. Profit is:

`sales - refund - platform fee - advertising fee - shipping fee - product cost`

The expected enterprise result is sales 900.00, refund 70.00, fees 122.00, product cost 375.00, and profit 333.00. The platform's certified aggregate does not expose the fee subtypes or cost separately, so the script verifies certified sales/refund/total fees/profit and independently checks cost as `sales - refund - fees - profit`.

`missing-order-id.csv.template` deliberately omits the required `order_id` field. It must be stored as a failed-quality run and must not be confirmable or published.

The seed also proves the cross-scope contracts: the single order source is bound to both stores, and a generated `.pbix` copy of `pbix-parser-fallback.fixture` is expected to fail automatic parsing, accept manual metadata, and bind to both stores plus the enterprise's platform account. The fixture is plain synthetic text; no customer PBIX is stored in Git.

Run `./scripts/demo-seed.sh` against a demo-initialized healthy stack. It authenticates with `DEMO_ADMIN_EMAIL` and `DEMO_ADMIN_PASSWORD`, or with an explicit `DEMO_ACCESS_TOKEN`. Run `./scripts/demo-reset.sh --confirm` only when it is acceptable to delete this Compose project's named volumes and recreate the entire demo; that command is the only workflow that opts into `DEMO_AUTO_SETUP=true`.
