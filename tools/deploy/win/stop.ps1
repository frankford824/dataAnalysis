# 停服务。换代码之前必须先停。
#
# 不只是停任务：serve.ps1 是个死循环，任务停了它拉起来的 python 可能还在，
# 那个 python 的当前目录压着 app\ledger，目录就删不掉。所以任务和子进程一起收。

[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'

$Task = 'LedgerHarness'
$Port = 8000

$t = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
if ($t) {
  Stop-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
  Write-Output '  已停任务'
}

# 兜底：按端口找监听进程收掉。任务停了但子进程漏下来的情况是存在的。
for ($i = 1; $i -le 20; $i++) {
  $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
       Select-Object -First 1
  if (-not $c) { break }
  Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
  Start-Sleep -Milliseconds 500
}

$c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($c) { throw "端口 $Port 上还有进程，停不干净" }
Write-Output ("  端口 " + $Port + " 已空")
