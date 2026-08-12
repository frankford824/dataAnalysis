# 注册成开机自启的服务、放行端口、收紧密钥权限、停掉旧服务的自启。
#
# 需要管理员。可重复执行：任务和防火墙规则都是先删后建，不会像上一代那样
# 攒出 72 条同名规则。
#
# 明确不做的事：不删旧应用的任何文件，不动它 2.9 GB 的数据，不清理它遗留的
# 防火墙规则（那些规则指向 5173，和本服务无关，留着由人决定）。

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Root = 'D:\ledger'
$Port = 8000
$Task = 'LedgerHarness'
$Rule = 'Ledger 记账服务 8000'

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw '要管理员权限'
}

Write-Output '== 注册计划任务 =='
# 为什么是 AtStartup + SYSTEM，而不是像上一代那样 AtLogon：
# 这台机器没开自动登录，绑登录触发的服务在无人登录时根本不存在，注销一次就没了。
# 旧任务最后一次以 0xC000013A（被强杀）结束，就是这个毛病。
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' +
             (Join-Path $Root 'bin\serve.ps1') + '"')
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
  -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $Task -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings `
  -Description '记账与核算服务。开机自启，进程崩溃由 serve.ps1 自行退避重启。' -Force | Out-Null
Write-Output ('  已注册 ' + $Task + '（开机启动，身份 SYSTEM，无运行时长上限）')

Write-Output ''
Write-Output '== 放行端口 =='
Get-NetFirewallRule -DisplayName $Rule -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $Rule -Direction Inbound -Action Allow `
  -Protocol TCP -LocalPort $Port -Profile Any `
  -Description '局域网访问记账服务' | Out-Null
$n = (Get-NetFirewallRule -DisplayName $Rule | Measure-Object).Count
Write-Output ('  ' + $Rule + '：' + $n + ' 条（就该是 1 条）')

Write-Output ''
Write-Output '== 收紧密钥权限 =='
foreach ($p in @((Join-Path $Root 'secrets'))) {
  if (-not (Test-Path $p)) { Write-Output ('  跳过（不存在）：' + $p); continue }
  $acl = Get-Acl $p
  $acl.SetAccessRuleProtection($true, $false)   # 断掉继承，父目录的 Users 读权限不再生效
  $acl.Access | ForEach-Object { $acl.RemoveAccessRule($_) | Out-Null }
  # 继承标志只对目录有意义，给文件加会直接抛 "没有可以设置的标志"。
  $inherit = if (Test-Path $p -PathType Container) { 'ContainerInherit,ObjectInherit' } else { 'None' }
  foreach ($who in @('NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators', "$env:COMPUTERNAME\sxf")) {
    $rule = New-Object Security.AccessControl.FileSystemAccessRule(
      $who, 'FullControl', $inherit, 'None', 'Allow')
    $acl.AddAccessRule($rule)
  }
  Set-Acl -Path $p -AclObject $acl
  Write-Output ('  已限定为 SYSTEM / Administrators / sxf：' + $p)
}

Write-Output ''
Write-Output '== 旧服务 FinanceAgentV1 =='
$old = Get-ScheduledTask -TaskName 'FinanceAgentV1' -ErrorAction SilentlyContinue
if ($old) {
  if ($old.State -eq 'Running') {
    Stop-ScheduledTask -TaskName 'FinanceAgentV1'
    Write-Output '  正在运行，已停止本次运行'
  }
  Disable-ScheduledTask -TaskName 'FinanceAgentV1' | Out-Null
  Write-Output '  已禁用自启（任务定义保留，文件与数据一个字节没动）'
} else {
  Write-Output '  找不到，跳过'
}

Write-Output ''
Write-Output '注册完了。起服务用 bin\start.ps1（它会等到真的能答话）。'
