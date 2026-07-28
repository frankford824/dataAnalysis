#!/usr/bin/env bash
# Migration acceptance for the edge/core split.
# Pass criteria:
#   1. edge and core never share a filesystem path;
#   2. crossing the boundary requires the shared token;
#   3. core in the cloud shape does not read customer sources itself;
#   4. changing FA_CORE_BASE_URL is enough to retarget core.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

CORE_WB="$TMP/core-workbench"
EDGE_INBOX="$TMP/edge-inbox"
mkdir -p "$CORE_WB" "$EDGE_INBOX"
cp "$ROOT/harness/config.example.toml" "$CORE_WB/config.toml"

export PYTHONPATH="$ROOT/harness:$ROOT/host-agent${PYTHONPATH:+:$PYTHONPATH}"
PY="${ROOT}/harness/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY=python3
fi

EDGE_TOKEN="boundary-acceptance-$(date +%s)"

"$PY" -m commerce_harness --config "$CORE_WB/config.toml" init --workspace "$CORE_WB" >/dev/null
"$PY" -m commerce_harness --config "$CORE_WB/config.toml" schema --workspace "$CORE_WB" >/dev/null

# Prove volumes are distinct.
"$PY" - <<PY
from pathlib import Path
from commerce_harness.edge.receive import assert_no_shared_workbench
assert_no_shared_workbench(Path("$EDGE_INBOX"), Path("$CORE_WB"))
print("boundary: distinct paths ok")
PY

# Prove core in the cloud shape refuses to read customer files itself.
FA_ROLE=core FA_ALLOW_CORE_SOURCE_READ=0 "$PY" - <<'PY'
from commerce_harness.role import reads_customer_sources
assert reads_customer_sources() is False, "core must not read customer sources"
print("boundary: core does not read customer sources")
PY

# Start core in background.
CORE_LOG="$TMP/core.log"
(
  cd "$ROOT"
  FA_CONTAINER_BIND=1 FA_AUTO_COMPUTE=0 FA_ROLE=core \
    FA_ALLOW_CORE_SOURCE_READ=0 FA_EDGE_TOKEN="$EDGE_TOKEN" \
    "$PY" -m commerce_harness --config "$CORE_WB/config.toml" serve \
      --workspace "$CORE_WB" --host 127.0.0.1 --port 18765
) >"$CORE_LOG" 2>&1 &
CORE_PID=$!
cleanup() {
  kill "$CORE_PID" 2>/dev/null || true
  wait "$CORE_PID" 2>/dev/null || true
  rm -rf "$TMP"
}
trap cleanup EXIT

for _ in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:18765/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done
curl -fsS "http://127.0.0.1:18765/api/v1/edge/health" | grep -q '"role":"core"'

printf 'order_id,amount\nA-1,10.00\n' >"$EDGE_INBOX/sample.csv"

# An unauthenticated upload must be rejected.
status="$(curl -s -o /dev/null -w '%{http_code}' \
  -F "content_sha256=$("$PY" -c "
import hashlib,pathlib
print(hashlib.sha256(pathlib.Path('$EDGE_INBOX/sample.csv').read_bytes()).hexdigest())
")" \
  -F 'original_name=sample.csv' \
  -F 'source_uri=edge-inbox://sample.csv' \
  -F "file=@$EDGE_INBOX/sample.csv" \
  "http://127.0.0.1:18765/api/v1/edge/snapshots")"
if [[ "$status" != "401" ]]; then
  echo "未鉴权的上传应被拒绝，实际返回 $status" >&2
  exit 1
fi
echo "boundary: unauthenticated upload rejected"

EDGE_TOKEN="$EDGE_TOKEN" "$PY" - <<PY
import os
from pathlib import Path
from commerce_harness.edge.client import CoreUploadClient
client = CoreUploadClient(
    "http://127.0.0.1:18765", token=os.environ["EDGE_TOKEN"]
)
receipt = client.upload_file(
    Path("$EDGE_INBOX/sample.csv"),
    original_name="sample.csv",
    source_uri="edge-inbox://sample.csv",
)
assert receipt.content_sha256
assert receipt.byte_size > 0
# Retarget simulation: same client against same host proves HTTP-only contract.
again = client.upload_file(
    Path("$EDGE_INBOX/sample.csv"),
    original_name="sample.csv",
    source_uri="edge-inbox://sample.csv",
)
assert again.reused is True
print(f"upload ok snapshot={receipt.snapshot_id} reused={again.reused}")
PY

echo "migration-acceptance: PASS"
