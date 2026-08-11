# 采样 60 秒：整机 CPU、服务进程占了多少核、内存。
# 用来回答"算账时到底吃没吃满这台机器"。
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

$Port = 8000
$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
Write-Output ("逻辑核 " + $cores + " 个")
Write-Output "秒  整机CPU%  服务占核数  服务内存MB"

$c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
     Select-Object -First 1
if (-not $c) { throw '服务没在监听' }
$pid_ = $c.OwningProcess

$prev = (Get-Process -Id $pid_).TotalProcessorTime
$peakCpu = 0.0
$peakCores = 0.0

for ($i = 1; $i -le 60; $i++) {
  Start-Sleep -Seconds 1
  $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
  if (-not $proc) { Write-Output ("$i  服务进程不在了"); break }
  $now = $proc.TotalProcessorTime
  $used = ($now - $prev).TotalSeconds        # 这一秒里用掉的 CPU 秒数 = 占了几个核
  $prev = $now
  $total = (Get-CimInstance Win32_Processor).LoadPercentage
  if ($total -gt $peakCpu) { $peakCpu = $total }
  if ($used -gt $peakCores) { $peakCores = $used }
  Write-Output ("{0,2}  {1,7}  {2,10}  {3,10}" -f $i, $total,
                [math]::Round($used, 1), [math]::Round($proc.WorkingSet64 / 1MB))
}

Write-Output ""
Write-Output ("峰值：整机 CPU " + $peakCpu + "%，服务吃到 " + [math]::Round($peakCores, 1) +
              " 个核（共 " + $cores + " 个）")
