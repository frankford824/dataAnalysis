#!/usr/bin/env bash
# 在 finance-win 上跑一段 PowerShell。
#
# 为什么绕这么一圈：
#   那台机器默认 shell 是 cmd.exe，直接 ssh "powershell -Command ..." 会被 cmd 按
#   | 和 & 切开；从 stdin 喂进去，PowerShell 按 GBK 读，中文一乱连语法都错；
#   -EncodedCommand 能解决前两个，但 base64 过了 cmd 的 8191 字符命令行上限。
#   所以：传成带 BOM 的 UTF-8 文件再执行。BOM 是关键，没它 PowerShell 5.1 仍按
#   本地 ANSI 读文件，中文照样乱。
#
# 落地位置固定在 REMOTE_DIR，只放我们自己的东西，不碰机器上任何既有文件。
set -euo pipefail

KEY=/home/wsfwk/.ssh/finance_agent_deploy
HOST=sxf@192.168.0.155
REMOTE_DIR='C:/Users/sxf/ledger-deploy'

script="${1:?用法: run.sh <脚本.ps1> [超时秒]}"
timeout_s="${2:-180}"
name=$(basename "$script")

ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 \
             -o ServerAliveCountMax=8 -i "$KEY" "$HOST" "$@"; }

# 加 UTF-8 BOM。
tmp=$(mktemp)
printf '\xEF\xBB\xBF' > "$tmp"
cat "$script" >> "$tmp"

ssh_ "if not exist \"${REMOTE_DIR//\//\\}\" mkdir \"${REMOTE_DIR//\//\\}\"" >/dev/null 2>&1 || true
scp -q -o BatchMode=yes -i "$KEY" "$tmp" "$HOST:$REMOTE_DIR/$name"
rm -f "$tmp"

exec timeout "$timeout_s" ssh -o BatchMode=yes -o ConnectTimeout=10 \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=8 -i "$KEY" "$HOST" \
  "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"${REMOTE_DIR//\//\\}\\$name\""
