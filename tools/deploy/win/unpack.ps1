# 解开代码包，全量替换 D:\ledger\app。
#
# 为什么先删再解：旧版本删掉的模块如果留在盘上，import 照样能找到它，
# 于是线上跑的是一份仓库里已经不存在的代码——这种问题查起来要命。
# 只删 app 一个目录，工作区、密钥、日志都在它外面，动不到。

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

# 给模型目录留指纹。下次部署前 preserve.ps1 拿它比对，能认出「这个文件是有人在
# 界面上改的」还是「本来就是这一版发出去的样子」。没有这份指纹，两者长得一样。
$Models   = Join-Path $App 'models'
$Manifest = Join-Path $Root 'shipped-model.sha256'
if (Test-Path $Models) {
  $lines = Get-ChildItem $Models -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($Models.Length).TrimStart('\')
    (Get-FileHash $_.FullName -Algorithm SHA256).Hash + '  ' + $rel
  }
  Set-Content -Path $Manifest -Value $lines -Encoding UTF8
  Write-Output ("  给 " + $lines.Count + " 个模型文件留了指纹")
}
