# 换代码之前，先看看服务器上的模型配置有没有被人改过。
#
# 为什么需要这一步：模型目录跟着代码一起走 app\，而部署是整个删掉 app 再解包。
# 可界面上「登记一家新店」「配法人主体」「接一张新表」「传提成配置」写的都是这个
# 目录。也就是说，有人在界面上配了半天，下一次部署会一声不响地全抹掉——
# 而且不会报错，只表现为「我配的店怎么没了」。
#
# 这里做两件事：
#   一、无论如何先备份一份。几百 KB 的代价，换掉「配置没了且找不回来」这种事。
#   二、和上次部署留下的指纹比对。对不上就说明有人在服务器上改过，停下来问人——
#       部署脚本自己判断不了那些改动是该保留还是该被覆盖。
#
# 加 -Force 表示「我知道会覆盖，继续」。

param([switch]$Force)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Root     = 'D:\ledger'
$Models   = Join-Path $Root 'app\models'
$Manifest = Join-Path $Root 'shipped-model.sha256'
$Backups  = Join-Path $Root 'model-backups'

if (-not (Test-Path $Models)) {
  Write-Output '  还没部署过模型，没什么可保的'
  exit 0
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$dest  = Join-Path $Backups $stamp
New-Item -ItemType Directory -Force -Path $dest | Out-Null
Copy-Item $Models -Destination $dest -Recurse -Force
$n = (Get-ChildItem $dest -Recurse -File | Measure-Object).Count
Write-Output ("  备份 " + $n + " 个文件到 model-backups\" + $stamp)

# 只留最近十次。备份是为了出事能找回来，不是为了攒满磁盘。
Get-ChildItem $Backups -Directory | Sort-Object Name -Descending |
  Select-Object -Skip 10 | ForEach-Object {
    Remove-Item $_.FullName -Recurse -Force
    Write-Output ("  清掉旧备份 " + $_.Name)
  }

if (-not (Test-Path $Manifest)) {
  Write-Output '  上一版没留指纹，这次比不了（这一版部署完就有了）'
  exit 0
}

function Get-Fingerprints($root) {
  $map = @{}
  Get-ChildItem $root -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($root.Length).TrimStart('\')
    $map[$rel] = (Get-FileHash $_.FullName -Algorithm SHA256).Hash
  }
  return $map
}

$was = @{}
Get-Content $Manifest | ForEach-Object {
  if ($_ -match '^([0-9A-Fa-f]{64})\s+(.+)$') { $was[$Matches[2]] = $Matches[1].ToUpper() }
}
$now = Get-Fingerprints $Models

$drift = @()
foreach ($k in $now.Keys) {
  if (-not $was.ContainsKey($k))       { $drift += "  新增  $k" }
  elseif ($was[$k] -ne $now[$k])       { $drift += "  改过  $k" }
}
foreach ($k in $was.Keys) {
  if (-not $now.ContainsKey($k))       { $drift += "  删了  $k" }
}

if ($drift.Count -eq 0) {
  Write-Output '  服务器上的模型和上次部署时一模一样，放心覆盖'
  exit 0
}

Write-Output ''
Write-Output '  服务器上的模型配置在上次部署之后被改过：'
$drift | ForEach-Object { Write-Output $_ }
Write-Output ''
Write-Output ('  这些改动多半是有人在界面上配的（登记店铺、配主体、接表、传提成配置）。')
Write-Output ('  已备份到 model-backups\' + $stamp + '，但接下来的部署会用仓库里那一份整个覆盖。')

if ($Force) {
  Write-Output '  带了 -Force，继续覆盖。'
  exit 0
}
Write-Output '  先把这些改动同步回仓库，或者确认可以丢弃之后带 -Force 重跑。'
exit 3
