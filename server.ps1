# 控制 stats_server.py：.\server.ps1 {start|stop|restart|status} [服务参数，如 --port 18764]
# 须经本脚本启动，stop/restart 才能定位进程；PID 与日志存于 .stats/（派生数据，可删）。
# start/status 在服务未就绪（索引构建中，HTTP 尚未监听）时阻塞到就绪。
param(
  [Parameter(Position = 0)] [string]$Command,
  [Parameter(ValueFromRemainingArguments = $true)] [string[]]$ServerArgs
)

$ErrorActionPreference = 'Stop'

$Root     = $PSScriptRoot
$StatsDir = Join-Path $Root '.stats'
$PidFile  = Join-Path $StatsDir 'server.pid'
$OutLog   = Join-Path $StatsDir 'server.log'
$ErrLog   = Join-Path $StatsDir 'server.err.log'
$Python   = Join-Path $Root '.venv/Scripts/python.exe'
$Server   = Join-Path $Root 'stats_server.py'
# 命令行可能显示 R: 或 C: 两种路径形态，只按文件名匹配
$ServerRe = 'stats_server\.py'

# 结果行着色：成功绿色、失败/错误红色（错误仍走 stderr）；地址、日志路径等信息行不着色
function Write-Ok {
  param([string]$Message)
  Write-Host $Message -ForegroundColor Green
}

function Write-Err {
  param([string]$Message)
  # 无控制台的会话（如非交互 SSH）里 [Console]::Color 不可用，降级为无色
  $colored = $false
  try {
    $prev = [Console]::Color
    [Console]::Color = 'Red'
    $colored = $true
  }
  catch { }
  [Console]::Error.WriteLine($Message)
  if ($colored) { [Console]::Color = $prev }
}

# 输出存活的服务 PID；无则返回 $null
function Get-ServerPid {
  if (-not (Test-Path -LiteralPath $PidFile)) { return $null }
  $raw = (Get-Content -LiteralPath $PidFile -Raw).Trim()
  if ($raw -notmatch '^\d+$') { return $null }
  # 存活判断用 Get-Process（进程快照）而非 CIM：进程被杀后 WMI 视图会残留数秒（幽灵）
  if (-not (Get-Process -Id ([int]$raw) -ErrorAction SilentlyContinue)) { return $null }
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $raw" -ErrorAction SilentlyContinue
  if (-not $proc) { return $null }
  if ($proc.CommandLine -notmatch $ServerRe) { return $null }
  return [int]$raw
}

# 查找运行中的服务进程（真服务，不是 re-exec 的父进程）；无则 $null
function Find-RunningServer {
  $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match $ServerRe })
  foreach ($p in $procs) {
    $child = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($p.ProcessId)" -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match $ServerRe } |
      Select-Object -First 1
    if ($child) { return $child }
  }
  if ($procs.Count -gt 0) { return $procs[0] }
  return $null
}

function Write-StartupFailure {
  Write-Err '启动失败，最近日志：'
  foreach ($path in @($OutLog, $ErrLog)) {
    if (Test-Path -LiteralPath $path) {
      Get-Content -LiteralPath $path -Tail 5 | ForEach-Object { Write-Err $_ }
    }
  }
}

# 从服务参数（或运行中进程的命令行）推导访问地址，口径同 stats_server.py 的启动打印
function Get-ServerUrl {
  param([string[]]$Tokens)
  $port = 18763
  $hostName = '127.0.0.1'
  for ($i = 0; $i -lt $Tokens.Count; $i++) {
    if ($Tokens[$i] -eq '--port' -and $i + 1 -lt $Tokens.Count) { $port = $Tokens[$i + 1] }
    elseif ($Tokens[$i] -eq '--host' -and $i + 1 -lt $Tokens.Count) { $hostName = $Tokens[$i + 1] }
  }
  if ($hostName -in @('0.0.0.0', '::')) { $hostName = '127.0.0.1' }
  if ($hostName -like '*:*') { $hostName = "[$hostName]" }
  "http://${hostName}:$port"
}

# 探测服务并返回索引元数据；未就绪时 $null
function Get-ServerMeta {
  param([string]$Url)
  try {
    $response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 -Uri "$Url/api/meta"
    if ($response.StatusCode -eq 200) { return $response.Content | ConvertFrom-Json }
  }
  catch { }
  return $null
}

# 阻塞直到 /api/meta 就绪（索引构建期间 HTTP 尚未监听）；等待期间进程退出则按启动失败处理
function Wait-ServerReady {
  param([string]$Url)
  $notified = $false
  while (-not (Get-ServerMeta -Url $Url)) {
    if (-not $notified) {
      Write-Output '索引构建中，等待就绪…'
      $notified = $true
    }
    if (-not (Get-ServerPid)) {
      Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
      Write-StartupFailure
      exit 1
    }
    Start-Sleep -Seconds 1
  }
}

function Start-Server {
  param([string[]]$ServerArgs = @())
  $existing = Get-ServerPid
  if (-not $existing) {
    # PID 文件缺失/失效但进程还活着（如别的会话直接启动过）：认回来，stop/status 才能定位
    $running = Find-RunningServer
    if ($running) {
      Set-Content -LiteralPath $PidFile -Value $running.ProcessId
      $existing = [int]$running.ProcessId
    }
  }
  if ($existing) {
    # 地址从运行中进程的实际命令行解析，避免与本次传入的参数不一致
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $existing" -ErrorAction SilentlyContinue
    $tokens = if ($proc) { @($proc.CommandLine -split '\s+') } else { @($ServerArgs) }
    $url = Get-ServerUrl -Tokens $tokens
    Write-Ok "已在运行 (PID $existing)，打开 $url"
    Wait-ServerReady -Url $url
    Write-Ok '状态: 运行中'
    return
  }
  if (-not (Test-Path -LiteralPath $Python)) {
    Write-Err "未找到 $Python，请先运行 uv sync"
    exit 1
  }
  New-Item -ItemType Directory -Force -Path $StatsDir | Out-Null
  Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
  # 经 WMI 起中间 pwsh（由 svchost 托管，不在 SSH 会话的 job object 里）：
  # 直接 Start-Process 起的进程会在 OpenSSH 会话结束时被整组杀掉（本机实测，同 mc.ps1）。
  # 中间层起完 python 即退出，python 被托管给系统，不受会话生命周期影响。
  $pwsh = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
  if (-not $pwsh) { $pwsh = 'powershell' }
  # -u：stdout 重定向到文件时 Python 默认块缓冲，日志会迟迟不落盘
  $argTokens = @('-u', $Server) + @($ServerArgs)
  $argList = ($argTokens | ForEach-Object { "'" + ($_ -replace "'", "''") + "'" }) -join ', '
  # 中间层继承 WMI 服务（系统）环境，其 ACP 未必是 UTF-8；强制 python 输出编码，避免日志乱码
  $innerCmd = "`$env:PYTHONIOENCODING='utf-8'; Start-Process -FilePath '$Python' -ArgumentList @($argList) -WindowStyle Hidden -RedirectStandardOutput '$OutLog' -RedirectStandardError '$ErrLog'"
  $midCmd = "`"$pwsh`" -NoProfile -WindowStyle Hidden -Command `"$innerCmd`""
  try {
    $wmi = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $midCmd; CurrentDirectory = $Root }
    if ($null -eq $wmi -or $wmi.ReturnValue -ne 0) {
      throw "Win32_Process.Create failed (return=$($wmi.ReturnValue))"
    }
  }
  catch {
    Write-Err "启动失败: $($_.Exception.Message)"
    exit 1
  }
  # WMI 只返回中间层 PID，服务进程按命令行认回来（与 stop/status 同一套判定）；
  # 按创建时间过滤，防止误认旧实例
  $before = Get-Date
  $launcher = $null
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Milliseconds 250
    $launcher = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
      Where-Object { $_.CommandLine -match $ServerRe -and $_.CreationDate -gt $before } |
      Select-Object -First 1
    if ($launcher) { break }
  }
  if (-not $launcher) {
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    Write-StartupFailure
    exit 1
  }
  # uv 的 venv python.exe 对脚本文件会 re-exec 出子进程（父进程是等待壳）；子进程才是真服务
  $serverProc = $launcher
  for ($i = 0; $i -lt 10; $i++) {
    $child = Get-CimInstance Win32_Process -Filter "ParentProcessId = $($launcher.ProcessId)" -ErrorAction SilentlyContinue |
      Where-Object { $_.Name -eq 'python.exe' } |
      Select-Object -First 1
    if ($child) { $serverProc = $child; break }
    Start-Sleep -Milliseconds 200
  }
  Set-Content -LiteralPath $PidFile -Value $serverProc.ProcessId
  Start-Sleep -Seconds 1
  if (Get-ServerPid) {
    $url = Get-ServerUrl -Tokens $ServerArgs
    Write-Ok "已启动 (PID $($serverProc.ProcessId))"
    Write-Output "打开 $url"
    Wait-ServerReady -Url $url
    Write-Ok '状态: 运行中'
    Write-Output "日志: $OutLog / $ErrLog"
  }
  else {
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    Write-StartupFailure
    exit 1
  }
}

function Stop-Server {
  $procId = Get-ServerPid
  if (-not $procId) {
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    Write-Output '未在运行'
    return
  }
  # uv 的 venv python re-exec 出子进程（父进程是等待壳）；成对杀掉，避免孤儿继续占端口
  $targets = @($procId)
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
  if ($proc) {
    $related = @()
    $related += Get-CimInstance Win32_Process -Filter "ParentProcessId = $($proc.ProcessId)" -ErrorAction SilentlyContinue
    if ($proc.ParentProcessId) {
      $related += Get-CimInstance Win32_Process -Filter "ProcessId = $($proc.ParentProcessId)" -ErrorAction SilentlyContinue
    }
    foreach ($r in $related) {
      if ($r -and $r.Name -eq 'python.exe' -and $r.CommandLine -match $ServerRe) {
        $targets += [int]$r.ProcessId
      }
    }
  }
  foreach ($id in ($targets | Select-Object -Unique)) {
    Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
  }
  $i = 0
  while ($i -lt 5 -and (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
    Start-Sleep -Seconds 1
    $i++
  }
  if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
    Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  }
  Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
  Write-Ok "已停止 (PID $procId)"
}

function Show-ServerStatus {
  $procId = Get-ServerPid
  if (-not $procId) {
    Write-Err '未在运行'
    exit 1
  }
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $procId" -ErrorAction SilentlyContinue
  $tokens = if ($proc) { @($proc.CommandLine -split '\s+') } else { @() }
  $url = Get-ServerUrl -Tokens $tokens
  Write-Ok "运行中 (PID $procId)"
  Write-Output "打开 $url"
  $meta = Get-ServerMeta -Url $url
  if (-not $meta) {
    Wait-ServerReady -Url $url
    $meta = Get-ServerMeta -Url $url
  }
  # ConvertFrom-Json 已把 ISO 时间戳转成 DateTime，两种形态都兼容
  $updated = $meta.updated
  if ($updated -is [string]) { $updated = [DateTime]::Parse($updated) }
  Write-Ok "响应正常（索引更新于 $($updated.ToString('yyyy-MM-dd HH:mm:ss'))，$($meta.turns) 轮）"
  Write-Output "日志: $OutLog / $ErrLog"
}

switch ($Command) {
  'start'   { Start-Server -ServerArgs $ServerArgs }
  'stop'    { Stop-Server }
  'restart' { Stop-Server; Start-Server -ServerArgs $ServerArgs }
  'status'  { Show-ServerStatus }
  default {
    Write-Err "用法: $PSCommandPath {start|stop|restart|status} [stats_server.py 参数]"
    exit 2
  }
}
exit 0
