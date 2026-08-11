# 解开代码包，全量替换 D:\ledger\app。
#
# 为什么先删再解：旧版本删掉的模块如果留在盘上，import 照样能找到它，
# 于是线上跑的是一份仓库里已经不存在的代码——这种问题查起来要命。
# 只删 app 一个目录，工作区、密钥、日志、鉴权都在它外面，动不到。

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Root = 'D:\ledger'
$App  = Join-Path $Root 'app'
$Tar  = Join-Path $Root 'payload.tar'

if (-not (Test-Path $Tar)) { throw "代码包不在：$Tar" }

if (Test-Path $App) { Remove-Item $App -Recurse -Force }
New-Item -ItemType Directory -Force -Path $App | Out-Null

& tar.exe -xf $Tar -C $App
if ($LASTEXITCODE -ne 0) { throw "解包失败，tar 退出码 $LASTEXITCODE" }
Remove-Item $Tar -Force

$n = (Get-ChildItem $App -Recurse -File | Measure-Object).Count
Write-Output ("  解出 " + $n + " 个文件")

# 关键路径逐个点名。少一个后面报的错会离现场很远。
foreach ($rel in @('VERSION', 'ledger\ledger\api.py', 'ledger\requirements.txt',
                   'ledger\requirements-dev.txt', 'ledger\tests',
                   'models\cn-ecommerce\model.yaml',
                   'models\cn-ecommerce\responsibility.csv')) {
  $ok = Test-Path (Join-Path $App $rel)
  Write-Output ("  " + $rel.PadRight(42) + $ok)
  if (-not $ok) { throw "缺文件：$rel" }
}

Write-Output ("  这一版是 " + (Get-Content (Join-Path $App 'VERSION') -Raw).Trim())
