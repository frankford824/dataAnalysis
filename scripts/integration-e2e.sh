#!/bin/sh
set -eu

[ ! -f .env ] || { set -a; . ./.env; set +a; }
./scripts/smoke-test.sh
./scripts/demo-seed.sh

echo "Checking the guest-token RLS contract includes enterprise and store scope..."
demo_context="${DEMO_ARTIFACT_DIR:-artifacts/demo}/context.json"
enterprise_id="$(DEMO_CONTEXT="$demo_context" python3 -c 'import json,os; print(json.load(open(os.environ["DEMO_CONTEXT"]))["enterprise_id"])')"
store_id="$(DEMO_CONTEXT="$demo_context" python3 -c 'import json,os; print(json.load(open(os.environ["DEMO_CONTEXT"]))["stores"]["Trail Shop"])')"
source_id="$(DEMO_CONTEXT="$demo_context" python3 -c 'import json,os; print(json.load(open(os.environ["DEMO_CONTEXT"]))["source_id"])')"
pbix_id="$(DEMO_CONTEXT="$demo_context" python3 -c 'import json,os; print(json.load(open(os.environ["DEMO_CONTEXT"]))["pbix_id"])')"
token="$(SUPERSET_GUEST_TOKEN_SECRET="${SUPERSET_GUEST_TOKEN_SECRET:-development-guest-secret-change-me}" \
  ./scripts/superset-guest-token.py --enterprise-id "$enterprise_id" --store-id "$store_id")"
TOKEN="$token" ENTERPRISE_ID="$enterprise_id" STORE_ID="$store_id" python3 - <<'PY'
import base64, json, os
part = os.environ["TOKEN"].split(".")[1]
part += "=" * (-len(part) % 4)
payload = json.loads(base64.urlsafe_b64decode(part))
clause = payload["rls_rules"][0]["clause"]
if os.environ["ENTERPRISE_ID"] not in clause or os.environ["STORE_ID"] not in clause:
    raise SystemExit(f"guest RLS scope is incomplete: {clause}")
if payload["exp"] - payload["iat"] > 600:
    raise SystemExit("guest token lifetime exceeds the delivery limit")
PY
docker compose exec -T \
  -e DEMO_GUEST_TOKEN="$token" \
  -e DEMO_ENTERPRISE_ID="$enterprise_id" \
  -e DEMO_STORE_ID="$store_id" \
  superset python - <<'PY'
import os

from superset.app import create_app

app = create_app()
with app.app_context():
    security_manager = app.appbuilder.sm
    parsed = security_manager.parse_jwt_guest_token(os.environ["DEMO_GUEST_TOKEN"])
    guest = security_manager.get_guest_user_from_token(parsed)
    if len(guest.rls) != 1:
        raise SystemExit(f"Superset did not load the guest RLS rule: {guest.rls}")
    clause = guest.rls[0]["clause"]
    if os.environ["DEMO_ENTERPRISE_ID"] not in clause or os.environ["DEMO_STORE_ID"] not in clause:
        raise SystemExit(f"Superset guest RLS scope is incomplete: {clause}")
PY

echo "Checking PostgreSQL certified view through the dedicated read-only role..."
docker compose exec -T postgres sh -ec '
  PGPASSWORD="$ANALYTICS_READER_PASSWORD" psql -h 127.0.0.1 \
    -U analytics_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "SELECT enterprise_id, store_id, sum(revenue) FROM certified.sales GROUP BY enterprise_id, store_id" >/dev/null
'
if docker compose exec -T postgres sh -ec '
  PGPASSWORD="$ANALYTICS_READER_PASSWORD" psql -h 127.0.0.1 \
    -U analytics_reader -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
    -c "UPDATE certified.sales SET revenue = 0" >/dev/null 2>&1
'; then
  echo "analytics_reader unexpectedly wrote through the certified view" >&2
  exit 1
fi

echo "Checking multi-store source and PBIX scope bindings in PostgreSQL..."
docker compose exec -T postgres psql \
  -U "${POSTGRES_USER:-commerce}" -d "${POSTGRES_DB:-commerce}" -v ON_ERROR_STOP=1 -At \
  -v source_id="$source_id" -v pbix_id="$pbix_id" <<'SQL' | grep -qx '2|3|manually_registered'
SELECT
  (SELECT count(*) FROM source_bindings WHERE source_definition_id = :'source_id' AND archived_at IS NULL),
  (SELECT count(*) FROM model_scope_bindings WHERE model_asset_id = :'pbix_id' AND archived_at IS NULL),
  (SELECT validation_status FROM model_assets WHERE id = :'pbix_id');
SQL

echo "Checking Superset metadata contains the real certified dataset and dashboard..."
docker compose exec -T postgres psql \
  -U "${POSTGRES_USER:-commerce}" -d superset -v ON_ERROR_STOP=1 -At <<'SQL' | grep -qx '1|1|1'
SELECT
  (SELECT count(*) FROM dbs WHERE database_name = 'Certified Commerce Data'),
  (SELECT count(*) FROM tables WHERE schema = 'certified' AND table_name = 'sales'),
  (SELECT count(*) FROM dashboards WHERE slug = 'commerce-overview');
SQL

echo "Checking MinIO has original and normalized evidence objects..."
object_count="$(docker compose run --rm --no-deps --entrypoint /bin/sh minio-bootstrap -ec '
  mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
  for bucket in "$S3_RAW_BUCKET" "$S3_INTERMEDIATE_BUCKET"; do
    mc find "local/$bucket"
  done | wc -l
')"
[ "$object_count" -ge 6 ] || { echo "expected at least six raw/normalized demo objects, got $object_count" >&2; exit 1; }

if docker compose ps --status running --services | grep -qx litellm; then
  echo "LiteLLM unexpectedly started in the default no-AI stack" >&2
  exit 1
fi

./scripts/diagnose.sh
echo "PostgreSQL/MinIO/API/Web/Superset golden-path integration passed"
