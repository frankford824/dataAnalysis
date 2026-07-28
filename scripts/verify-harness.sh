#!/usr/bin/env bash
set -euo pipefail

base_url="${FA_HARNESS_URL:-http://127.0.0.1:8765}"
task_tmp="$(mktemp -d)"
cleanup() {
  rm -f -- "$task_tmp/ready.txt" "$task_tmp/status.json" \
    "$task_tmp/progress.json" "$task_tmp/reviews.headers" \
    "$task_tmp/reviews.csv"
  rmdir -- "$task_tmp"
}
trap cleanup EXIT

curl -fsS "$base_url/readyz" -o "$task_tmp/ready.txt"
if [[ "$(tr -d '\r\n' <"$task_tmp/ready.txt")" != "ready" ]]; then
  echo "就绪检查没有返回 ready" >&2
  exit 1
fi

curl -fsS "$base_url/api/v1/status" -o "$task_tmp/status.json"
curl -fsS "$base_url/api/v1/progress" -o "$task_tmp/progress.json"
curl -fsS -D "$task_tmp/reviews.headers" \
  "$base_url/api/v1/reviews.csv" -o "$task_tmp/reviews.csv"

python3 - "$task_tmp" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
status = json.loads((root / "status.json").read_text(encoding="utf-8"))
progress = json.loads((root / "progress.json").read_text(encoding="utf-8"))
headers = (root / "reviews.headers").read_text(encoding="iso-8859-1").lower()
csv_bytes = (root / "reviews.csv").read_bytes()

expected = {
    "mode": "real",
    "reconciliationMode": "platform_wallet",
    "bankCashStatus": "not_applicable",
    "readOnlySourceEnforced": True,
}
for field, value in expected.items():
    if status.get(field) != value:
        raise SystemExit(f"状态字段 {field} 不符合预期：{status.get(field)!r}")

if "content-type: text/csv" not in headers:
    raise SystemExit("确认清单不是 CSV MIME")
if 'filename="reconciliation-reviews.csv"' not in headers:
    raise SystemExit("确认清单下载文件名不正确")
if not csv_bytes.startswith(b"\xef\xbb\xbf"):
    raise SystemExit("确认清单缺少 Excel 兼容的 UTF-8 BOM")
header = csv_bytes.decode("utf-8-sig").splitlines()[0]
if header != (
    "item_type,item_id,subject,store,period,"
    "amount,decision,business_reason"
):
    raise SystemExit("确认清单表头不正确")

print("Harness 自检通过")
print(f"- schema: {status.get('schemaVersion')}")
print("- 核对范围: 订单 + 支付宝/微信平台钱包")
print("- 银行流水: 当前不适用")
print(f"- 原始快照: {progress.get('sourceCount')}")
print(f"- 待处理记录: {progress.get('unresolvedCount')}")
print("- 确认清单: CSV / UTF-8 BOM / 文件名均正确")
PY
