#!/bin/sh
set -eu

demo_admin_email_override="${DEMO_ADMIN_EMAIL:-}"
demo_admin_password_override="${DEMO_ADMIN_PASSWORD:-}"
[ ! -f .env ] || { set -a; . ./.env; set +a; }
if [ -n "$demo_admin_email_override" ]; then DEMO_ADMIN_EMAIL="$demo_admin_email_override"; export DEMO_ADMIN_EMAIL; fi
if [ -n "$demo_admin_password_override" ]; then DEMO_ADMIN_PASSWORD="$demo_admin_password_override"; export DEMO_ADMIN_PASSWORD; fi
api_url="${BACKEND_URL:-http://127.0.0.1:${BACKEND_PORT:-8000}}"
artifact_dir="${DEMO_ARTIFACT_DIR:-artifacts/demo}"
mkdir -p "$artifact_dir"

access_token="${DEMO_ACCESS_TOKEN:-}"
if [ -z "$access_token" ]; then
  : "${DEMO_ADMIN_EMAIL:?DEMO_ADMIN_EMAIL is required unless DEMO_ACCESS_TOKEN is set}"
  : "${DEMO_ADMIN_PASSWORD:?DEMO_ADMIN_PASSWORD is required unless DEMO_ACCESS_TOKEN is set}"
  login_payload="$(python3 - <<'PY'
import json, os
print(json.dumps({"email": os.environ["DEMO_ADMIN_EMAIL"], "password": os.environ["DEMO_ADMIN_PASSWORD"]}))
PY
)"
  login="$(curl -fsS -H 'Content-Type: application/json' --data "$login_payload" "$api_url/api/v1/auth/login")"
  access_token="$(printf '%s' "$login" | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"
fi

request() {
  method="$1"
  path="$2"
  shift 2
  if [ -n "${enterprise_id:-}" ]; then
    curl -fsS -X "$method" \
      -H "Authorization: Bearer $access_token" \
      "$@" "$api_url$path"
  else
    curl -fsS -X "$method" \
      -H "Authorization: Bearer $access_token" \
      "$@" "$api_url$path"
  fi
}

json_value() {
  expression="$1"
  python3 -c "import json,sys; value=json.load(sys.stdin); print($expression)"
}

echo "Locating the seeded demonstration enterprise..."
me="$(curl -fsS -H "Authorization: Bearer $access_token" "$api_url/api/v1/auth/me")"
enterprise_id="$(printf '%s' "$me" | json_value "value['enterprise_id']")"

enterprises="$(request GET /api/v1/enterprises)"
if ! printf '%s' "$enterprises" | python3 -c 'import json,sys; raise SystemExit(0 if any(x["name"] == "Example Home Goods" for x in json.load(sys.stdin)) else 1)'; then
  request POST /api/v1/enterprises \
    -H 'Content-Type: application/json' \
    --data '{"name":"Example Home Goods","activation_at":"2026-01-01T00:00:00Z"}' >/dev/null
fi
enterprises="$(request GET /api/v1/enterprises)"
second_enterprise_id="$(printf '%s' "$enterprises" | json_value "next(x['id'] for x in value if x['name']=='Example Home Goods')")"

stores="$(request GET /api/v1/stores)"
alpha_id="$(printf '%s' "$stores" | json_value "next((x['id'] for x in value if x['name']=='Trail Shop'), '')")"
if [ -z "$alpha_id" ]; then
  alpha="$(request POST /api/v1/stores -H 'Content-Type: application/json' --data '{"name":"Trail Shop","status":"active","activation_at":"2026-01-01T00:00:00Z","effective_from":"2026-01-01T00:00:00Z","external_store_id":"trail-demo"}')"
  alpha_id="$(printf '%s' "$alpha" | json_value "value['id']")"
fi
beta_id="$(printf '%s' "$stores" | json_value "next((x['id'] for x in value if x['name']=='Coast Outlet'), '')")"
if [ -z "$beta_id" ]; then
  beta="$(request POST /api/v1/stores -H 'Content-Type: application/json' --data '{"name":"Coast Outlet","status":"active","activation_at":"2026-01-01T00:00:00Z","effective_from":"2026-01-01T00:00:00Z","external_store_id":"coast-demo"}')"
  beta_id="$(printf '%s' "$beta" | json_value "value['id']")"
fi

sources="$(request GET /api/v1/sources)"
source_id="$(printf '%s' "$sources" | json_value "next((x['id'] for x in value if x['name']=='Standard order export'), '')")"
if [ -z "$source_id" ]; then
  source="$(request POST /api/v1/sources -H 'Content-Type: application/json' --data '{"name":"Standard order export","status":"active","activation_at":"2026-01-01T00:00:00Z","effective_from":"2026-01-01T00:00:00Z","file_types":["csv","xlsx","zip"],"recognition":{"required_headers":["order_id","occurred_at"]},"field_aliases":{},"coverage_time_field":"occurred_at","data_granularity":"day","arrival_frequency":"daily","expected_rows":6,"required":true,"dedupe_keys":["order_id"],"validations":[{"type":"required_field","field":"order_id"}],"store_field":"store_id"}')"
  source_id="$(printf '%s' "$source" | json_value "value['id']")"
fi

source_bindings="$(request GET /api/v1/source-bindings)"
for binding_spec in "$alpha_id|Trail Shop" "$beta_id|Coast Outlet"; do
  binding_store_id="${binding_spec%%|*}"
  binding_store_name="${binding_spec#*|}"
  if ! printf '%s' "$source_bindings" | SOURCE_ID="$source_id" STORE_ID="$binding_store_id" python3 -c '
import json, os, sys
items = json.load(sys.stdin)
raise SystemExit(0 if any(item["source_definition_id"] == os.environ["SOURCE_ID"] and item["scope_type"] == "store" and item["scope_id"] == os.environ["STORE_ID"] for item in items) else 1)
'; then
    binding_payload="$(SOURCE_ID="$source_id" STORE_ID="$binding_store_id" STORE_NAME="$binding_store_name" python3 - <<'PY'
import json, os
print(json.dumps({"name": f"Standard orders for {os.environ['STORE_NAME']}", "source_definition_id": os.environ["SOURCE_ID"], "scope_type": "store", "scope_id": os.environ["STORE_ID"], "status": "active", "effective_from": "2026-01-01T00:00:00Z"}))
PY
)"
    request POST /api/v1/source-bindings -H 'Content-Type: application/json' --data "$binding_payload" >/dev/null
  fi
done

cp examples/demo/pbix-parser-fallback.fixture "$artifact_dir/manual-fallback.pbix"
assets="$(request GET /api/v1/model-assets)"
pbix_id="$(printf '%s' "$assets" | json_value "next((x['id'] for x in value if x['name']=='Demo PBIX manual fallback'), '')")"
if [ -z "$pbix_id" ]; then
  pbix="$(request POST /api/v1/model-assets/pbix \
    -F 'name=Demo PBIX manual fallback' \
    -F "file=@$artifact_dir/manual-fallback.pbix;type=application/octet-stream")"
  pbix_id="$(printf '%s' "$pbix" | json_value "value['id']")"
  pbix_status="$(printf '%s' "$pbix" | json_value "value['validation_status']")"
  [ "$pbix_status" = manual_required ] || { echo "invalid PBIX did not fall back to manual registration" >&2; exit 1; }
  request POST "/api/v1/model-assets/$pbix_id/manual-metadata" \
    -H 'Content-Type: application/json' \
    --data '{"tables":["Sales","Stores"],"measures":["Revenue","Profit"],"expected_inputs":["Standard order export"],"note":"Demonstration of graceful manual registration after parser fallback"}' >/dev/null
fi

platforms="$(request GET /api/v1/platforms)"
platform_id="$(printf '%s' "$platforms" | json_value "value[0]['id']")"
model_bindings="$(request GET /api/v1/model-scope-bindings)"
for scope_spec in "store|$alpha_id|Trail Shop" "store|$beta_id|Coast Outlet" "platform_account|$platform_id|Primary platform"; do
  scope_type="${scope_spec%%|*}"
  scope_rest="${scope_spec#*|}"
  scope_id="${scope_rest%%|*}"
  scope_name="${scope_rest#*|}"
  if ! printf '%s' "$model_bindings" | MODEL_ID="$pbix_id" SCOPE_TYPE="$scope_type" SCOPE_ID="$scope_id" python3 -c '
import json, os, sys
items = json.load(sys.stdin)
raise SystemExit(0 if any(item["model_asset_id"] == os.environ["MODEL_ID"] and item["scope_type"] == os.environ["SCOPE_TYPE"] and item["scope_id"] == os.environ["SCOPE_ID"] for item in items) else 1)
'; then
    scope_payload="$(MODEL_ID="$pbix_id" SCOPE_TYPE="$scope_type" SCOPE_ID="$scope_id" SCOPE_NAME="$scope_name" python3 - <<'PY'
import json, os
print(json.dumps({"name": f"Demo PBIX scope: {os.environ['SCOPE_NAME']}", "model_asset_id": os.environ["MODEL_ID"], "scope_type": os.environ["SCOPE_TYPE"], "scope_id": os.environ["SCOPE_ID"], "status": "active", "effective_from": "2026-01-01T00:00:00Z"}))
PY
)"
    request POST /api/v1/model-scope-bindings -H 'Content-Type: application/json' --data "$scope_payload" >/dev/null
  fi
done

models="$(request GET /api/v1/semantic-models)"
model_id="$(printf '%s' "$models" | json_value "next((x['id'] for x in value if x.get('industry_template')=='ecommerce_standard' and x.get('status')=='published'), '')")"
if [ -z "$model_id" ]; then
  model="$(request POST /api/v1/semantic-models -H 'Content-Type: application/json' --data '{"name":"E-commerce standard model","status":"published","effective_from":"2026-01-01T00:00:00Z","industry_template":"ecommerce_standard","definition":{"facts":["sales","refunds","fees","costs"],"dimensions":["store","date"]},"quality_gates":[{"key":"reconciliation","required":true}]}')"
  model_id="$(printf '%s' "$model" | json_value "value['id']")"
fi
metrics="$(request GET /api/v1/metrics)"
for spec in 'sales|Sales|sum(revenue)' 'refund|Refund|sum(refund)' 'platform_fee|Platform fee|sum(platform_fee)' 'advertising_fee|Advertising fee|sum(advertising_fee)' 'shipping_fee|Shipping fee|sum(shipping_fee)' 'product_cost|Product cost|sum(product_cost)' 'profit|Profit|sum(revenue-refund-platform_fee-advertising_fee-shipping_fee-product_cost)'; do
  key="${spec%%|*}"; rest="${spec#*|}"; name="${rest%%|*}"; expression="${rest#*|}"
  if ! printf '%s' "$metrics" | METRIC_KEY="$key" python3 -c 'import json,os,sys; raise SystemExit(0 if any(x.get("key")==os.environ["METRIC_KEY"] and x.get("status")=="published" for x in json.load(sys.stdin)) else 1)'; then
    metric_payload="$(METRIC_KEY="$key" METRIC_NAME="$name" METRIC_EXPRESSION="$expression" MODEL_ID="$model_id" python3 - <<'PY'
import json, os
print(json.dumps({"name": os.environ["METRIC_NAME"], "status": "published", "effective_from": "2026-01-01T00:00:00Z", "semantic_model_id": os.environ["MODEL_ID"], "key": os.environ["METRIC_KEY"], "expression": os.environ["METRIC_EXPRESSION"]}))
PY
)"
    request POST /api/v1/metrics -H 'Content-Type: application/json' --data "$metric_payload" >/dev/null
  fi
done
dashboards="$(request GET /api/v1/dashboards)"
if ! printf '%s' "$dashboards" | python3 -c 'import json,sys; raise SystemExit(0 if any(x.get("external_id")=="741fec6d-5c6b-4f81-8df2-ec59cf16fb55" for x in json.load(sys.stdin)) else 1)'; then
  request POST /api/v1/dashboards -H 'Content-Type: application/json' --data '{"name":"Commerce overview","status":"published","effective_from":"2026-01-01T00:00:00Z","bi_adapter":"superset","external_id":"741fec6d-5c6b-4f81-8df2-ec59cf16fb55","embed_url":"/superset/embedded/741fec6d-5c6b-4f81-8df2-ec59cf16fb55","definition":{"template":"ecommerce_overview"}}' >/dev/null
fi

sed -e "s/STORE_ALPHA/$alpha_id/g" -e "s/STORE_BETA/$beta_id/g" examples/demo/orders.csv.template > "$artifact_dir/orders.csv"
sed -e "s/STORE_ALPHA/$alpha_id/g" -e "s/STORE_BETA/$beta_id/g" examples/demo/fees.csv.template > "$artifact_dir/fees.csv"
sed -e "s/STORE_ALPHA/$alpha_id/g" examples/demo/missing-order-id.csv.template > "$artifact_dir/missing-order-id.csv"

upload_and_publish() {
  file="$1"
  upload="$(request POST /api/v1/ingestions/upload \
    -F "source_definition_id=$source_id" \
    -F "file=@$file;type=text/csv")"
  run_id="$(printf '%s' "$upload" | json_value "value['id']")"
  status="$(printf '%s' "$upload" | json_value "value['status']")"
  case "$status" in
    awaiting_confirmation)
      request POST "/api/v1/ingestions/$run_id/confirm" \
        -H 'Content-Type: application/json' \
        --data '{"accepted":true,"note":"Matched to the independent June control totals"}' >/dev/null
      request POST "/api/v1/ingestions/$run_id/publish" >/dev/null
      request POST "/api/v1/ingestions/$run_id/lock" >/dev/null
      ;;
    quality_passed)
      request POST "/api/v1/ingestions/$run_id/publish" >/dev/null
      request POST "/api/v1/ingestions/$run_id/lock" >/dev/null
      ;;
    published) request POST "/api/v1/ingestions/$run_id/lock" >/dev/null ;;
    locked) ;;
    *) echo "cannot publish $file from status $status" >&2; exit 1 ;;
  esac
  printf '%s' "$run_id"
}

orders_run="$(upload_and_publish "$artifact_dir/orders.csv")"
fees_run="$(upload_and_publish "$artifact_dir/fees.csv")"

# Same source hash must return the original run and never add certified rows.
duplicate="$(request POST /api/v1/ingestions/upload \
  -F "source_definition_id=$source_id" \
  -F "file=@$artifact_dir/orders.csv;type=text/csv")"
duplicate_id="$(printf '%s' "$duplicate" | json_value "value['id']")"
[ "$duplicate_id" = "$orders_run" ] || { echo "duplicate upload created a new run" >&2; exit 1; }

invalid="$(request POST /api/v1/ingestions/upload \
  -F "source_definition_id=$source_id" \
  -F "file=@$artifact_dir/missing-order-id.csv;type=text/csv")"
invalid_status="$(printf '%s' "$invalid" | json_value "value['status']")"
[ "$invalid_status" = quality_failed ] || { echo "missing required field was not stopped by quality gates" >&2; exit 1; }

certified="$(request GET '/api/v1/analytics/overview?date_from=2026-06-01T00%3A00%3A00Z&date_to=2026-06-30T23%3A59%3A59Z')"
printf '%s' "$certified" > "$artifact_dir/certified.json"
DEMO_ALPHA_ID="$alpha_id" DEMO_BETA_ID="$beta_id" python3 - "$artifact_dir/certified.json" <<'PY'
import json
import os
import sys
from decimal import Decimal

with open(sys.argv[1], encoding="utf-8") as handle:
    result = json.load(handle)

keys = ("revenue", "refund", "fees", "product_cost", "profit")
actual = {
    row["store_id"]: {key: Decimal(str(row[key])) for key in keys}
    for row in result["by_store"]
}

expected = {
    os.environ["DEMO_ALPHA_ID"]: {"revenue": "400", "refund": "20", "fees": "55", "product_cost": "170", "profit": "155"},
    os.environ["DEMO_BETA_ID"]: {"revenue": "500", "refund": "50", "fees": "67", "product_cost": "205", "profit": "178"},
}
for store_id, values in expected.items():
    if store_id not in actual:
        raise SystemExit(f"certified output is missing store {store_id}")
    for key, expected_value in values.items():
        if actual[store_id][key] != Decimal(expected_value):
            raise SystemExit(f"{store_id} {key}: expected {expected_value}, got {actual[store_id][key]}")

enterprise = {key: Decimal(str(result["metrics"][key])) for key in keys}
expected_enterprise = {"revenue": Decimal("900"), "refund": Decimal("70"), "fees": Decimal("122"), "product_cost": Decimal("375"), "profit": Decimal("333")}
if enterprise != expected_enterprise:
    raise SystemExit(f"enterprise reconciliation mismatch: {enterprise}")
PY

isolated="$(request GET '/api/v1/analytics/overview' -H "X-Act-As-Enterprise-ID: $second_enterprise_id")"
printf '%s' "$isolated" | python3 -c '
import json, sys
result = json.load(sys.stdin)
if result.get("metrics", {}).get("row_count") != 0 or result.get("by_store"):
    raise SystemExit("the second demonstration enterprise can see certified rows from the first")
'

cat > "$artifact_dir/context.json" <<EOF
{"enterprise_id":"$enterprise_id","isolation_enterprise_id":"$second_enterprise_id","stores":{"Trail Shop":"$alpha_id","Coast Outlet":"$beta_id"},"source_id":"$source_id","pbix_id":"$pbix_id","runs":{"orders":"$orders_run","fees":"$fees_run"}}
EOF

echo "demo seed passed: sales=900 refund=70 fees=122 product_cost=375 profit=333"
echo "context: $artifact_dir/context.json"
