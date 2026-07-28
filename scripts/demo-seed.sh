#!/bin/sh
set -eu

admin_email_override="${DEMO_ADMIN_EMAIL:-}"
admin_password_override="${DEMO_ADMIN_PASSWORD:-}"
[ ! -f .env ] || { set -a; . ./.env; set +a; }
[ -z "$admin_email_override" ] || DEMO_ADMIN_EMAIL="$admin_email_override"
[ -z "$admin_password_override" ] || DEMO_ADMIN_PASSWORD="$admin_password_override"
: "${DEMO_ADMIN_EMAIL:?DEMO_ADMIN_EMAIL is required}"
: "${DEMO_ADMIN_PASSWORD:?DEMO_ADMIN_PASSWORD is required}"

api_url="${BACKEND_URL:-http://127.0.0.1:${BACKEND_PORT:-8000}}"
artifact_dir="${DEMO_ARTIFACT_DIR:-artifacts/demo}"
mkdir -p "$artifact_dir"
cookie_jar="$(mktemp)"
trap 'rm -f "$cookie_jar"' EXIT

json_value() { expression="$1"; python3 -c "import json,sys; value=json.load(sys.stdin); print($expression)"; }
payload="$(DEMO_ADMIN_EMAIL="$DEMO_ADMIN_EMAIL" DEMO_ADMIN_PASSWORD="$DEMO_ADMIN_PASSWORD" python3 - <<'PY'
import json, os
print(json.dumps({"email": os.environ["DEMO_ADMIN_EMAIL"], "password": os.environ["DEMO_ADMIN_PASSWORD"]}))
PY
)"
curl -fsS -c "$cookie_jar" -H 'Content-Type: application/json' --data "$payload" "$api_url/api/v1/auth/login" >/dev/null
request() { method="$1"; path="$2"; shift 2; curl -fsS -b "$cookie_jar" -c "$cookie_jar" -X "$method" "$@" "$api_url$path"; }

me="$(request GET /api/v1/auth/me)"
enterprise_id="$(printf '%s' "$me" | json_value "value['enterprise_id']")"

enterprises="$(request GET /api/v1/enterprises)"
if ! printf '%s' "$enterprises" | python3 -c 'import json,sys; raise SystemExit(0 if any(x["name"]=="Example Home Goods" for x in json.load(sys.stdin)) else 1)'; then
  request POST /api/v1/enterprises -H 'Content-Type: application/json' --data '{"name":"Example Home Goods","activation_at":"2026-01-01T00:00:00Z"}' >/dev/null
  enterprises="$(request GET /api/v1/enterprises)"
fi
second_enterprise_id="$(printf '%s' "$enterprises" | json_value "next(x['id'] for x in value if x['name']=='Example Home Goods')")"

platform_id="$(request GET /api/v1/platforms | json_value "value[0]['id']")"
stores="$(request GET /api/v1/stores)"
alpha_id="$(printf '%s' "$stores" | json_value "next((x['id'] for x in value if x['name']=='Trail Shop'), '')")"
if [ -z "$alpha_id" ]; then
  first_store_id="$(printf '%s' "$stores" | json_value "value[0]['id']")"
  alpha="$(request PATCH "/api/v1/stores/$first_store_id" -H 'Content-Type: application/json' --data '{"name":"Trail Shop","external_store_id":"trail-demo"}')"
  alpha_id="$(printf '%s' "$alpha" | json_value "value['id']")"
fi
stores="$(request GET /api/v1/stores)"
beta_id="$(printf '%s' "$stores" | json_value "next((x['id'] for x in value if x['name']=='Coast Outlet'), '')")"
if [ -z "$beta_id" ]; then
  beta_payload="$(PLATFORM_ID="$platform_id" python3 - <<'PY'
import json, os
print(json.dumps({"name":"Coast Outlet","status":"active","activation_at":"2026-01-01T00:00:00Z","platform_account_id":os.environ["PLATFORM_ID"],"external_store_id":"coast-demo"}))
PY
)"
  beta="$(request POST /api/v1/stores -H 'Content-Type: application/json' --data "$beta_payload")"
  beta_id="$(printf '%s' "$beta" | json_value "value['id']")"
fi

sources="$(request GET /api/v1/sources)"
orders_source_id="$(printf '%s' "$sources" | json_value "next(x['id'] for x in value if x.get('source_kind')=='orders')")"
fees_source_id="$(printf '%s' "$sources" | json_value "next(x['id'] for x in value if x.get('source_kind')=='fees')")"
orders_source_logical="$(printf '%s' "$sources" | json_value "next(x['logical_id'] for x in value if x.get('source_kind')=='orders')")"

bindings="$(request GET /api/v1/source-bindings)"
for source_id in "$orders_source_id" "$fees_source_id"; do
  if ! printf '%s' "$bindings" | SOURCE_ID="$source_id" STORE_ID="$beta_id" python3 -c 'import json,os,sys; items=json.load(sys.stdin); raise SystemExit(0 if any(x["source_definition_id"]==os.environ["SOURCE_ID"] and x["scope_type"]=="store" and x["scope_id"]==os.environ["STORE_ID"] for x in items) else 1)'; then
    binding_payload="$(SOURCE_ID="$source_id" STORE_ID="$beta_id" python3 - <<'PY'
import json, os
print(json.dumps({"name":"Coast Outlet monthly data","source_definition_id":os.environ["SOURCE_ID"],"scope_type":"store","scope_id":os.environ["STORE_ID"],"status":"active"}))
PY
)"
    request POST /api/v1/source-bindings -H 'Content-Type: application/json' --data "$binding_payload" >/dev/null
  fi
done

sed -e "s/STORE_ALPHA/$alpha_id/g" -e "s/STORE_BETA/$beta_id/g" examples/demo/orders.csv.template > "$artifact_dir/orders.csv"
sed -e "s/STORE_ALPHA/$alpha_id/g" -e "s/STORE_BETA/$beta_id/g" examples/demo/fees.csv.template > "$artifact_dir/fees.csv"
sed -e "s/STORE_ALPHA/$alpha_id/g" examples/demo/missing-order-id.csv.template > "$artifact_dir/missing-order-id.csv"
sed 's/A-1001,2026-06-03T10:15:00Z,\([^,]*\),sale,120.00/A-1001,2026-06-03T10:15:00Z,\1,sale,110.00/' "$artifact_dir/orders.csv" > "$artifact_dir/orders-initial.csv"

upload() {
  source_id="$1"; file="$2"
  request POST /api/v1/ingestions/upload -F "source_definition_id=$source_id" -F "file=@$file;type=text/csv"
}
confirm_publish() {
  run_id="$1"
  request POST "/api/v1/ingestions/$run_id/confirm" -H 'Content-Type: application/json' --data '{"accepted":true,"note":"已与本月必需文件核对"}' >/dev/null
  request POST "/api/v1/ingestions/$run_id/publish" >/dev/null
}

orders_initial="$(upload "$orders_source_id" "$artifact_dir/orders-initial.csv")"
orders_initial_id="$(printf '%s' "$orders_initial" | json_value "value['id']")"
[ "$(printf '%s' "$orders_initial" | json_value "value['status']")" = quality_pending ] || { echo 'orders should wait for monthly fees' >&2; exit 1; }
fees_run="$(upload "$fees_source_id" "$artifact_dir/fees.csv")"
fees_run_id="$(printf '%s' "$fees_run" | json_value "value['id']")"
confirm_publish "$fees_run_id"
confirm_publish "$orders_initial_id"

orders_revision="$(upload "$orders_source_id" "$artifact_dir/orders.csv")"
orders_revision_id="$(printf '%s' "$orders_revision" | json_value "value['id']")"
confirm_publish "$orders_revision_id"
request POST "/api/v1/ingestions/$orders_revision_id/lock" >/dev/null
request POST "/api/v1/ingestions/$fees_run_id/lock" >/dev/null

duplicate="$(upload "$orders_source_id" "$artifact_dir/orders.csv")"
[ "$(printf '%s' "$duplicate" | json_value "value['id']")" = "$orders_revision_id" ] || { echo 'duplicate upload created another run' >&2; exit 1; }
[ "$(printf '%s' "$duplicate" | json_value "str(value.get('deduplicated', False)).lower()")" = true ] || { echo 'duplicate response was not explicit' >&2; exit 1; }

invalid="$(upload "$orders_source_id" "$artifact_dir/missing-order-id.csv")"
[ "$(printf '%s' "$invalid" | json_value "value['status']")" = quality_failed ] || { echo 'missing order id did not fail' >&2; exit 1; }

certified="$(request GET '/api/v1/analytics/overview?date_from=2026-06-01T00%3A00%3A00Z&date_to=2026-07-01T00%3A00%3A00Z')"
printf '%s' "$certified" > "$artifact_dir/certified.json"
python3 - "$artifact_dir/certified.json" <<'PY'
import json, sys
from decimal import Decimal
result=json.load(open(sys.argv[1], encoding='utf-8'))
metrics=result['metrics']
expected={'revenue':'900.0000','refund':'70.0000','fees':'122.0000','product_cost':'375.0000','profit':'333.0000'}
for key, value in expected.items():
    if Decimal(str(metrics[key])) != Decimal(value): raise SystemExit(f'{key}: expected {value}, got {metrics[key]}')
if metrics['row_count'] != 12 or metrics['order_count'] != 6:
    raise SystemExit(f"counts: expected rows=12 orders=6, got {metrics['row_count']} / {metrics['order_count']}")
PY

isolated="$(request GET '/api/v1/analytics/overview' -H "X-Act-As-Enterprise-ID: $second_enterprise_id")"
printf '%s' "$isolated" | python3 -c 'import json,sys; value=json.load(sys.stdin); raise SystemExit(1 if value.get("metrics",{}).get("row_count") or value.get("by_store") else 0)'

cp examples/demo/pbix-parser-fallback.fixture "$artifact_dir/manual-fallback.pbix"
pbix="$(request POST /api/v1/model-assets/pbix -F 'name=Demo PBIX manual fallback' -F "file=@$artifact_dir/manual-fallback.pbix;type=application/octet-stream")"
pbix_id="$(printf '%s' "$pbix" | json_value "value['id']")"
[ "$(printf '%s' "$pbix" | json_value "value['validation_status']")" = manual_required ] || { echo 'PBIX fallback was not manual' >&2; exit 1; }
request POST "/api/v1/model-assets/$pbix_id/manual-metadata" -H 'Content-Type: application/json' --data '{"tables":["Sales","Stores"],"measures":["Revenue","Profit"],"expected_inputs":["标准订单文件","标准平台费用文件"],"note":"解析失败后由实施人员登记"}' >/dev/null
for scope in "$alpha_id" "$beta_id"; do
  scope_payload="$(PBIX_ID="$pbix_id" STORE_ID="$scope" python3 - <<'PY'
import json, os
print(json.dumps({"name":"Demo PBIX store scope","model_asset_id":os.environ["PBIX_ID"],"scope_type":"store","scope_id":os.environ["STORE_ID"],"status":"active"}))
PY
)"
  request POST /api/v1/model-scope-bindings -H 'Content-Type: application/json' --data "$scope_payload" >/dev/null
done
platform_scope_payload="$(PBIX_ID="$pbix_id" PLATFORM_ID="$platform_id" python3 - <<'PY'
import json, os
print(json.dumps({"name":"Demo PBIX platform scope","model_asset_id":os.environ["PBIX_ID"],"scope_type":"platform_account","scope_id":os.environ["PLATFORM_ID"],"status":"active"}))
PY
)"
request POST /api/v1/model-scope-bindings -H 'Content-Type: application/json' --data "$platform_scope_payload" >/dev/null

cat > "$artifact_dir/context.json" <<EOF
{"enterprise_id":"$enterprise_id","isolation_enterprise_id":"$second_enterprise_id","stores":{"Trail Shop":"$alpha_id","Coast Outlet":"$beta_id"},"source_id":"$orders_source_id","source_logical_id":"$orders_source_logical","pbix_id":"$pbix_id","runs":{"orders_initial":"$orders_initial_id","orders":"$orders_revision_id","fees":"$fees_run_id"}}
EOF
echo 'demo seed passed: rows=12 orders=6 sales=900 refund=70 fees=122 product_cost=375 profit=333'
