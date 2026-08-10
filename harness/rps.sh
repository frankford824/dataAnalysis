#!/usr/bin/env bash
# Run a PowerShell script (read from stdin or $1 file) on the remote Windows box
# via -EncodedCommand, avoiding all cmd.exe quoting problems.
set -u
TMO="${TMO:-150}"
if [ $# -ge 1 ]; then SRC="$(cat "$1")"; else SRC="$(cat)"; fi
PRE='[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ErrorActionPreference="SilentlyContinue"; $ProgressPreference="SilentlyContinue";'
ENC=$(printf '%s\n%s' "$PRE" "$SRC" | iconv -f UTF-8 -t UTF-16LE | base64 -w0)
timeout "$TMO" ssh -o BatchMode=yes -i ~/.ssh/finance_agent_deploy sxf@192.168.0.155 \
  "powershell -NoProfile -EncodedCommand $ENC"
