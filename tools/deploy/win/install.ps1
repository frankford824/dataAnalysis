# 建 venv、装依赖。可重复执行。
#
# 只在 D:\ledger 下动土。不装任何系统级软件，不改注册表，不动机器上既有的任何文件。

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Root = 'D:\ledger'
$Venv = Join-Path $Root 'venv'
$Req  = Join-Path $Root 'app\ledger\requirements.txt'
$ReqDev = Join-Path $Root 'app\ledger\requirements-dev.txt'

function Say($m) { Write-Output ('  ' + $m) }

Write-Output '== 建 venv =='
$sys = (Get-Command python -ErrorAction Stop).Source
Say ('系统 Python: ' + $sys)
if (-not (Test-Path (Join-Path $Venv 'Scripts\python.exe'))) {
  & $sys -m venv $Venv
  if ($LASTEXITCODE -ne 0) { throw 'venv 建失败' }
  Say '已建'
} else {
  Say '已存在，复用'
}

$py = Join-Path $Venv 'Scripts\python.exe'
Say ('venv Python: ' + ((& $py --version) 2>&1))

Write-Output ''
Write-Output '== 装依赖 =='
# 走阿里云镜像。直连 pypi.org 也通，但国内拉 polars 这种带 Rust 二进制的大轮子会很慢。
$index = 'https://mirrors.aliyun.com/pypi/simple/'
& $py -m pip install --disable-pip-version-check --quiet --upgrade pip -i $index
if ($LASTEXITCODE -ne 0) { throw 'pip 自升级失败' }

& $py -m pip install --disable-pip-version-check -r $ReqDev -i $index
if ($LASTEXITCODE -ne 0) { throw '依赖装失败' }
Say '装好了（含 pytest，机器上能自己跑测试）'

Write-Output ''
Write-Output '== 核对关键依赖 =='
$check = @'
import importlib, sys
for name, want in [("polars", None), ("pydantic", None), ("python_calamine", None),
                   ("fastapi", None), ("uvicorn", None), ("ruamel.yaml", None)]:
    mod = importlib.import_module(name)
    v = getattr(mod, "__version__", "?")
    print(f"  {name:18} {v}")
import polars as pl
print(f"  polars 线程数        {pl.thread_pool_size()}")
print(f"  默认编码            {sys.getdefaultencoding()}")
import locale
print(f"  文件系统编码        {sys.getfilesystemencoding()}")
'@
$tmp = Join-Path $env:TEMP 'ledger_check.py'
Set-Content -Path $tmp -Value $check -Encoding UTF8
$env:PYTHONUTF8 = '1'
& $py $tmp
Remove-Item $tmp -Force -ErrorAction SilentlyContinue

Write-Output ''
Write-Output '== 模型能不能加载 =='
Push-Location (Join-Path $Root 'app\ledger')
$env:LEDGER_HOME = Join-Path $Root 'home'
& $py -c "from ledger.model.loader import load_model; from pathlib import Path; m = load_model(Path(r'$Root\app\models\cn-ecommerce')); print(f'  模型 {m.name}：{len(m.stores)} 家店、{len(m.templates)} 张模板、{len(m.metrics)} 个指标')"
if ($LASTEXITCODE -ne 0) { throw '模型加载失败' }
Pop-Location

Write-Output ''
Write-Output '安装完成。'
