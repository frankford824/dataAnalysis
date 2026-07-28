#!/bin/sh
set -eu

workbench="${FA_WORKBENCH:-/workbench}"
config="${FA_CONFIG:-${workbench}/config.toml}"

if [ ! -r "${config}" ]; then
  echo "fa-harness: 缺少只读配置文件 ${config}" >&2
  exit 1
fi

if [ -r /run/connection/config ] && [ -r /run/ssh-host/finance_agent_deploy ]; then
  cp /run/connection/config /home/harness/.ssh/config
  cp /run/ssh-host/finance_agent_deploy /home/harness/.ssh/finance_agent_deploy
  cp /run/ssh-host/known_hosts /home/harness/.ssh/known_hosts
  chmod 600 \
    /home/harness/.ssh/config \
    /home/harness/.ssh/finance_agent_deploy \
    /home/harness/.ssh/known_hosts
fi

if [ ! -f "${workbench}/.fa-workbench.json" ]; then
  python -m commerce_harness --config "${config}" init --workspace "${workbench}"
fi

python -m commerce_harness --config "${config}" schema --workspace "${workbench}"
exec python -m commerce_harness --config "${config}" serve \
  --workspace "${workbench}" \
  --host 0.0.0.0 \
  --port 8765
