#!/usr/bin/env bash
# 起一个演示用的界面服务。工作区在 /tmp/ledger-demo，不碰真账本。
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
pkill -9 -f "ledger.cli web" 2>/dev/null || true
sleep 1
export LEDGER_HOME=/tmp/ledger-demo
setsid nohup python -m ledger.cli web --port "${1:-8822}" > /tmp/ledger-web.log 2>&1 < /dev/null &
for _ in $(seq 1 30); do
  sleep 1
  if curl -sf --max-time 2 "http://127.0.0.1:${1:-8822}/api/bootstrap" > /dev/null; then
    echo "已就绪 http://127.0.0.1:${1:-8822}"
    exit 0
  fi
done
echo "没起来："
cat /tmp/ledger-web.log
exit 1
