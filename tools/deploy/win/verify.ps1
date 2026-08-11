# 验证两件不试就不知道的事：进程被杀了能不能自己回来、算账时吃不吃满核。
#
# 重启整台机器的验证做不了——这机器上有人在用，已经连续开机 58 天。杀进程能验证
# 守护循环这一半；开机自启那一半靠任务定义本身（AtStartup + SYSTEM）保证，
# 脚本最后把定义打印出来让人自己看。

[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Port = 8000
$Task = 'LedgerHarness'

function Listener {
  $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
       Select-Object -First 1
  if ($c) { return Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue }
  return $null
}

Write-Output '== 进程身份 =='
$p = Listener
if (-not $p) { throw "端口 $Port 没人监听，服务没起来" }
$owner = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" |
          Invoke-CimMethod -MethodName GetOwner)
Write-Output ('  pid ' + $p.Id + '  身份 ' + $owner.Domain + '\' + $owner.User)
Write-Output ('  以 SYSTEM 跑意味着注销、切用户都不影响它')

Write-Output ''
Write-Output '== 杀掉看它回不回来 =='
$before = $p.Id
Stop-Process -Id $before -Force
Write-Output ('  已强杀 pid ' + $before)

$back = $null
for ($i = 1; $i -le 40; $i++) {
  Start-Sleep -Milliseconds 500
  $cand = Listener
  if ($cand -and $cand.Id -ne $before) { $back = $cand; break }
}
if ($back) {
  Write-Output ('  ' + [math]::Round($i * 0.5, 1) + ' 秒后回来了，新 pid ' + $back.Id)
  try {
    $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $Port + '/') -TimeoutSec 15 -UseBasicParsing
    Write-Output ('  首页 HTTP ' + $r.StatusCode)
  } catch { Write-Output ('  但首页还打不开：' + $_.Exception.Message) }
} else {
  Write-Output '  20 秒内没回来，守护循环有问题'
}

Write-Output ''
Write-Output '== 开机自启的任务定义 =='
$t = Get-ScheduledTask -TaskName $Task
Write-Output ('  触发器: ' + ($t.Triggers | ForEach-Object { $_.CimClass.CimClassName }))
Write-Output ('  身份:   ' + $t.Principal.UserId + ' / ' + $t.Principal.LogonType +
              ' / ' + $t.Principal.RunLevel)
Write-Output ('  时长上限: ' + $t.Settings.ExecutionTimeLimit + '（PT0S = 不限）')
Write-Output ('  失败重启: 每 ' + $t.Settings.RestartInterval + ' 最多 ' +
              $t.Settings.RestartCount + ' 次')
