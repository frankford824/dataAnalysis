#!/usr/bin/env bash
# 把代码、模型、密钥和运维脚本送到 finance-win，装好依赖，注册成开机自启的服务。
#
# 可重复执行。每一步都只写 D:\ledger 以内的东西：
#   D:\ledger\app       代码与模型（每次全量替换）
#   D:\ledger\venv      虚拟环境
#   D:\ledger\home      工作区：留档的原始表、sqlite、llm.json（不动）
#   D:\ledger\secrets   模型密钥（不动）
#   D:\ledger\logs      日志（不动）
#
# 机器上其他任何路径——尤其 D:\财务 的 65 GB 业务数据和 D:\software\finance-agent
# 的 2.9 GB 历史结果——一个字节都不碰。
set -euo pipefail

KEY=/home/wsfwk/.ssh/finance_agent_deploy
HOST=sxf@192.168.0.155
REPO=/home/wsfwk/dataAnalysis
ROOT_WIN='D:\ledger'
ROOT_SCP='D:/ledger'

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
ssh_() { ssh -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 \
             -o ServerAliveCountMax=40 -i "$KEY" "$HOST" "$@"; }
put()  { scp -q -o BatchMode=yes -i "$KEY" "$1" "$HOST:$2"; }

# 把 .ps1 传上去时补 UTF-8 BOM。PowerShell 5.1 读没有 BOM 的文件按本地 ANSI 解，
# 脚本里的中文会烂成乱码，连语法都可能错。
put_ps1() {
  local src=$1 dst=$2 tmp
  tmp=$(mktemp)
  printf '\xEF\xBB\xBF' > "$tmp"
  cat "$src" >> "$tmp"
  put "$tmp" "$dst"
  rm -f "$tmp"
}

pwsh_() {  # 在远端跑一个已经传上去的脚本，后面跟脚本自己的参数
  local script=$1; shift
  ssh_ "powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"$ROOT_WIN\\bin\\$script\" $*"
}

stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT

step '打包代码与模型'
mkdir -p "$stage/app"
# 界面带的是构建产物（ledger/ledger/static），不是 Vue 源码，服务器上不需要 node。
# node_modules 有 162 MB，比整个包大一个量级，传过去也没有任何东西会读它。
tar -C "$REPO" -cf "$stage/app/payload.tar" \
  --exclude='.venv' --exclude='__pycache__' --exclude='.pytest_cache' \
  --exclude='*.pyc' --exclude='.git' --exclude='node_modules' \
  ledger models
# 列表落一次盘再查。`tar | grep -q` 在 pipefail 下是个陷阱：grep 命中就提前退出，
# tar 吃到 SIGPIPE 返回非零，于是「找到了」被判成「失败」。
tar -tf "$stage/app/payload.tar" > "$stage/listing.txt"
if grep -q 'ledger/ledger/static/assets/' "$stage/listing.txt"; then
  echo "  界面产物在包里"
else
  # 界面是构建出来的，源码改了不重新构建，产物就还是上一版。部署一份跑着旧界面的
  # 新后端，表现是接口全对、页面上什么都没变——这种问题很难往「忘了构建」上想。
  echo '  界面产物不在包里。先在 ledger/web 下跑一次 pnpm build。' >&2
  exit 1
fi
# 盖版本印。线上没有 git，不带这个文件过去，每笔账的运行记录都只能写「引擎 unknown」，
# 「回到哪一版」就永久无解。内容是打包这一刻本机 git 说的实话，脏就带 -dirty。
mkdir -p "$stage/stamp"
( cd "$REPO/ledger" && PYTHONPATH="$REPO/ledger" ./.venv/bin/python -c \
    "from ledger.version import engine_version; print(engine_version())" ) \
  > "$stage/stamp/VERSION"
tar -C "$stage/stamp" -rf "$stage/app/payload.tar" VERSION
echo "  版本印 $(cat "$stage/stamp/VERSION")"
tar -tf "$stage/app/payload.tar" > "$stage/listing.txt"
echo "  $(du -h "$stage/app/payload.tar" | cut -f1)  $(wc -l < "$stage/listing.txt") 个条目"
if LC_ALL=C grep -qP '[^\x00-\x7F]' "$stage/listing.txt"; then
  echo '  警告：包里有非 ASCII 文件名，Windows 上解包可能出问题'
fi

step '建目录'
# 这一步刻意不输出中文：它跑在 cmd 里，没机会先设 UTF-8 输出编码，中文会变乱码。
ssh_ "powershell -NoProfile -Command \"foreach (\$d in @('$ROOT_WIN','$ROOT_WIN\\app','$ROOT_WIN\\bin','$ROOT_WIN\\home','$ROOT_WIN\\logs','$ROOT_WIN\\secrets')) { New-Item -ItemType Directory -Force -Path \$d | Out-Null }; Write-Output ('  ok: ' + \$d)\""

step '传运维脚本'
for f in serve.ps1 install.ps1 register.ps1 status.ps1 unpack.ps1 preserve.ps1 stop.ps1 start.ps1 verify.ps1; do
  put_ps1 "$REPO/tools/deploy/win/$f" "$ROOT_SCP/bin/$f"
  echo "  bin\\$f"
done

step '保住服务器上的模型配置'
# 模型目录跟代码一起走 app\，而部署是整个删掉 app 再解包。可界面上登记店铺、
# 配主体、接新表、传提成配置，写的都是这个目录——不先看一眼，这些配置会在
# 下一次部署时一声不响地消失。带 FORCE=1 表示确认可以覆盖。
if ! pwsh_ preserve.ps1 ${FORCE:+-Force}; then
  echo
  echo '  停在这里：服务器上的模型配置和上次部署时不一样了（详情见上）。'
  echo '  把那些改动同步回仓库，或者确认可以丢弃之后 FORCE=1 重跑。'
  exit 3
fi

step '传代码'
put "$stage/app/payload.tar" "$ROOT_SCP/payload.tar"
# 先停。运行中的 python 即使不锁 .py 文件，它的当前目录也会压住 app\ledger；
# 更要紧的是，边跑边换代码会出现半新半旧的 import，那种状态算出来的账没人能解释。
# 停机代价是几秒，换来一条干净的版本边界。
if ssh_ "sc query state= all >nul 2>&1 & schtasks /query /tn LedgerHarness >nul 2>&1 && echo yes" | grep -q yes; then
  pwsh_ stop.ps1
else
  echo '  服务还没注册过，跳过停机'
fi
# 解包走独立脚本。多行 PowerShell 塞进 ssh 命令串没用——远端是 cmd.exe，
# 它只会执行第一行，后面的静静地不跑，还什么都不报。
pwsh_ unpack.ps1

step '模型密钥'
KEYFILE=$(python3 -c "
import json,pathlib
d=json.loads((pathlib.Path.home()/'.ledger/llm.json').read_text())
print(pathlib.Path(d['api_key_file']).expanduser())
")
put "$KEYFILE" "$ROOT_SCP/secrets/llm-api-key"
python3 - "$stage" <<'PY'
import json, pathlib, sys
stage = pathlib.Path(sys.argv[1])
src = json.loads((pathlib.Path.home() / '.ledger/llm.json').read_text())
out = {
    "enabled": src.get("enabled", True),
    "base_url": src["base_url"],
    "model": src["model"],
    "api_key_file": r"D:\ledger\secrets\llm-api-key",
    "timeout": src.get("timeout", 60),
}
(stage / 'llm.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"  模型 {out['model']}，密钥指向 {out['api_key_file']}")
PY
put "$stage/llm.json" "$ROOT_SCP/home/llm.json"

step '清掉旧鉴权'
# 登录机制已经撤了，服务也不再读 auth.json。留着一个没人读的密钥文件比删掉更糟：
# 下次有人看到它会以为访问还需要 token，照着它去查为什么自己的 token 不管用。
ssh_ "if exist \"$ROOT_WIN\\auth.json\" (del /q \"$ROOT_WIN\\auth.json\" & echo   删掉了 auth.json) else (echo   没有遗留的 auth.json)"
rm -f "$REPO/tools/deploy/.tokens.local.txt"

step '装依赖'
pwsh_ install.ps1

step '注册服务'
pwsh_ register.ps1

step '起服务'
pwsh_ start.ps1

step '状态'
pwsh_ status.ps1
