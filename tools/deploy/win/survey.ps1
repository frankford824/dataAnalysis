# 只读体检：部署前后都能跑，回答「这台机器现在到底是什么状况」。
#
# 不写文件、不停服务、不改配置。
#
# 之所以要有这么一份而不是临时敲命令：这台机器不是服务器，是有人每天在用的桌面
# （企业微信、抖店、网盘、双杀软）。每次动它之前得先知道装了什么、开着什么端口、
# 数据在哪，否则很容易把别人的东西碰坏。

[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'

function Head($t) { Write-Output ""; Write-Output ("=== " + $t + " ===") }

Head "机器"
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor
Write-Output ("系统:   " + $os.Caption + "  build " + $os.BuildNumber)
Write-Output ("CPU:    " + $cpu.Name + "  " + $cpu.NumberOfCores + " 核 / " +
              $cpu.NumberOfLogicalProcessors + " 线程")
Write-Output ("内存:   " + [math]::Round($cs.TotalPhysicalMemory/1GB,1) + " GB，空闲 " +
              [math]::Round($os.FreePhysicalMemory/1MB,1) + " GB")
Write-Output ("开机:   " + $os.LastBootUpTime + "（已运行 " +
              [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalDays,1) + " 天）")
Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue |
  Where-Object { $_.Free -gt 0 } |
  ForEach-Object { Write-Output ("盘 " + $_.Name + ":  已用 " +
    [math]::Round($_.Used/1GB,1) + " GB，剩 " + [math]::Round($_.Free/1GB,1) + " GB") }
Get-PhysicalDisk -ErrorAction SilentlyContinue |
  ForEach-Object { Write-Output ("介质:   " + $_.MediaType + " / " + $_.BusType) }

Head "容器化的前提（回答「能不能上 docker/k8s」）"
Write-Output ("CPU 支持虚拟化:   " + $cpu.VirtualizationFirmwareEnabled)
Write-Output ("SLAT（WSL2 必需）: " + $cpu.SecondLevelAddressTranslationExtensions)
Write-Output ("当前有 hypervisor: " + $cs.HypervisorPresent)
foreach ($n in @('Microsoft-Windows-Subsystem-Linux','VirtualMachinePlatform',
                 'Containers','Microsoft-Hyper-V')) {
  $s = Get-WindowsOptionalFeature -Online -FeatureName $n -ErrorAction SilentlyContinue
  $state = '查不到'
  if ($s) { $state = $s.State }
  Write-Output ("  " + $n.PadRight(38) + $state)
}
Write-Output "四项都 Disabled 意味着开启要重启机器，而且会在这台桌面的 OS 底下垫一层 hypervisor。"

Head "工具链"
foreach ($c in @('python','git','docker','wsl','tar','curl')) {
  $g = Get-Command $c -ErrorAction SilentlyContinue
  if ($g) { Write-Output ("  " + $c.PadRight(8) + $g.Source) }
  else    { Write-Output ("  " + $c.PadRight(8) + "没有") }
}

Head "监听端口（低于 30000）"
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -lt 30000 } |
  Select-Object LocalAddress, LocalPort,
    @{n='进程'; e={ (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName }} |
  Sort-Object LocalPort -Unique | Format-Table -AutoSize | Out-String -Width 120

Head "网络与防火墙"
Get-NetConnectionProfile -ErrorAction SilentlyContinue |
  Select-Object InterfaceAlias, NetworkCategory, IPv4Connectivity |
  Format-Table -AutoSize | Out-String
Get-NetFirewallProfile -ErrorAction SilentlyContinue |
  Select-Object Name, Enabled | Format-Table -AutoSize | Out-String
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
  Select-Object IPAddress, InterfaceAlias | Format-Table -AutoSize | Out-String

Head "本服务与旧服务"
foreach ($t in @('LedgerHarness', 'FinanceAgentV1')) {
  $x = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
  if ($x) {
    $i = Get-ScheduledTaskInfo -TaskName $t
    Write-Output ("  " + $t.PadRight(18) + $x.State + "  上次 " + $i.LastRunTime +
                  " 结果 " + $i.LastTaskResult)
    foreach ($a in $x.Actions) { Write-Output ("      -> " + $a.Execute + " " + $a.Arguments) }
  } else { Write-Output ("  " + $t.PadRight(18) + "没有") }
}

Head "不许碰的数据"
foreach ($p in @('D:\财务', 'D:\software\finance-agent')) {
  if (-not (Test-Path $p)) { Write-Output ("  " + $p + "  不存在"); continue }
  $f = Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue
  $sz = ($f | Measure-Object Length -Sum).Sum
  Write-Output ("  " + $p.PadRight(32) + ($f | Measure-Object).Count + " 个文件，" +
                [math]::Round($sz/1MB,1) + " MB")
}

Head "杀毒软件（会不会拦服务）"
Get-CimInstance -Namespace root\SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction SilentlyContinue |
  Select-Object displayName | Format-Table -AutoSize | Out-String
