# 起服务，并且等到它真的能答话为止。
#
# 「已下达启动指令」不等于「起来了」。不等就往下走，后面的验证会去问一个还没开门的
# 服务，报出来的错和真实原因差很远。

[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'

$Task = 'LedgerHarness'
$Port = 8000

Start-ScheduledTask -TaskName $Task
Write-Output '  已下达启动指令'

for ($i = 1; $i -le 60; $i++) {
  Start-Sleep -Milliseconds 500
  try {
    $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $Port + '/') -TimeoutSec 5 -UseBasicParsing
    if ($r.StatusCode -eq 200) {
      Write-Output ('  ' + [math]::Round($i * 0.5, 1) + ' 秒后能答话了')
      exit 0
    }
  } catch { }
}
throw "30 秒内没起来，看 D:\ledger\logs 下最新的日志"
