# 查服务状态。只读。
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Root = 'D:\ledger'
$Port = 8000
$Task = 'LedgerHarness'

Write-Output '== 计划任务 =='
$t = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
if ($t) {
  $i = Get-ScheduledTaskInfo -TaskName $Task
  Write-Output ('  状态: ' + $t.State)
  Write-Output ('  上次运行: ' + $i.LastRunTime + '  结果: ' + $i.LastTaskResult)
} else { Write-Output '  没注册' }

Write-Output ''
Write-Output '== 端口与进程 =='
$c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($c) {
  foreach ($x in $c) {
    $p = Get-Process -Id $x.OwningProcess -ErrorAction SilentlyContinue
    Write-Output ('  ' + $x.LocalAddress + ':' + $x.LocalPort + '  <- ' + $p.ProcessName +
                  ' pid=' + $p.Id + ' 内存 ' + [math]::Round($p.WorkingSet64/1MB) + 'MB')
  }
} else { Write-Output ('  ' + $Port + ' 没人监听') }

Write-Output ''
Write-Output '== 本机自测 =='
try {
  $sw = [Diagnostics.Stopwatch]::StartNew()
  $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $Port + '/') -TimeoutSec 20 -UseBasicParsing
  $sw.Stop()
  Write-Output ('  首页 HTTP ' + $r.StatusCode + '，' + $r.RawContentLength + ' 字节，' +
                [int]$sw.Elapsed.TotalMilliseconds + ' ms')
} catch { Write-Output ('  首页打不开: ' + $_.Exception.Message) }

Write-Output ''
Write-Output '== 工作区 =='
$home_ = Join-Path $Root 'home'
if (Test-Path $home_) {
  $f = Get-ChildItem (Join-Path $home_ 'files') -Recurse -File -ErrorAction SilentlyContinue
  $sz = ($f | Measure-Object Length -Sum).Sum
  Write-Output ('  留档文件 ' + ($f | Measure-Object).Count + ' 份，' + [math]::Round($sz/1MB,1) + ' MB')
  $db = Join-Path $home_ 'workspace.db'
  if (Test-Path $db) { Write-Output ('  workspace.db ' + [math]::Round((Get-Item $db).Length/1KB) + ' KB') }
} else { Write-Output '  还没有' }

Write-Output ''
Write-Output '== 最近日志 =='
$log = Get-ChildItem (Join-Path $Root 'logs') -Filter 'serve-*.log' -ErrorAction SilentlyContinue |
  Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) {
  Write-Output ('  ' + $log.FullName)
  Get-Content $log.FullName -Tail 25 | ForEach-Object { Write-Output ('  | ' + $_) }
} else { Write-Output '  还没有日志' }
