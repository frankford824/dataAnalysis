# 记账服务的守护启动器。由计划任务 LedgerHarness 在开机时以 SYSTEM 拉起。
#
# 为什么是一个循环而不是让 Windows 重启任务：计划任务的失败重试最小粒度是 1 分钟，
# 而且次数有上限；进程崩了要等一分钟才回来，崩够次数就彻底不回来了。这里自己带
# 指数退避，秒级恢复，而且永不放弃。真起不来的时候退避会拉到 60 秒一次，不会把
# CPU 烧在空转上，日志里也看得见它在反复重启。
#
# 只往 D:\ledger 里写东西。机器上其他任何目录都不碰。

$ErrorActionPreference = 'Continue'

$Root   = 'D:\ledger'
$Port   = 8000
$App    = Join-Path $Root 'app\ledger'      # 里面是 ledger 包，import 要从这里起
$Python = Join-Path $Root 'venv\Scripts\python.exe'
$LogDir = Join-Path $Root 'logs'

$env:LEDGER_HOME      = Join-Path $Root 'home'

# Windows 的默认编码是 cp936。模型是 UTF-8 的 YAML 和 CSV，店名科目名全是中文，
# 一旦按 cp936 读进来就是乱码，而且不会报错——它会算出一份"看着对"的账。
$env:PYTHONUTF8       = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

# 让 python 找得到 ledger 包，但不要把 app\ledger 当当前目录——进程的 cwd 会在
# 目录上压一个句柄，换代码时那个目录就删不掉，部署卡在这儿。
$env:PYTHONPATH = $App

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 日志留 30 天。不留的话磁盘慢慢被吃掉，留太久也没人看。
Get-ChildItem $LogDir -Filter 'serve-*.log' -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) } |
  Remove-Item -Force -ErrorAction SilentlyContinue

Set-Location $Root

$backoff = 2
while ($true) {
  $log = Join-Path $LogDir ('serve-' + (Get-Date -Format 'yyyyMMdd') + '.log')
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] 启动 uvicorn，端口 ' + $Port) | Add-Content -Path $log -Encoding UTF8

  $sw = [Diagnostics.Stopwatch]::StartNew()

  # 交给 cmd 做追加重定向：PowerShell 5.1 把子进程的 stderr 当错误记录收，
  # uvicorn 恰好把日志全写 stderr，直接接过来会被当成一片报错。
  $cmd = '"' + $Python + '" -m uvicorn ledger.api:app --host 0.0.0.0 --port ' + $Port +
         ' --workers 1 --timeout-keep-alive 75 >> "' + $log + '" 2>&1'
  & cmd.exe /c $cmd
  $code = $LASTEXITCODE

  $sw.Stop()
  $alive = [int]$sw.Elapsed.TotalSeconds
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] uvicorn 退出 code=' + $code +
   '，本次存活 ' + $alive + ' 秒') | Add-Content -Path $log -Encoding UTF8

  # 撑过一分钟算正常运行过，退避归零；起来就崩说明是配置或依赖问题，越退越慢。
  if ($alive -gt 60) { $backoff = 2 } else { $backoff = [Math]::Min($backoff * 2, 60) }
  ('[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $backoff + ' 秒后重启') |
    Add-Content -Path $log -Encoding UTF8
  Start-Sleep -Seconds $backoff
}
