[CmdletBinding()]
param(
    [ValidateSet("start", "stop", "restart", "status", "open", "logs", "menu")]
    [string]$Action = "status",
    [switch]$OpenBrowser
)

$ErrorActionPreference = "Stop"

try {
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $OutputEncoding = [System.Text.UTF8Encoding]::new($false)
} catch {
    # Older hosts can still run the manager; only Chinese console rendering may vary.
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$RunScriptPath = Join-Path $ProjectRoot "run.py"
$SetupScriptPath = Join-Path $ProjectRoot "setup.ps1"
$WorkspacePath = Join-Path $ProjectRoot "workspace"
$RuntimeDirectory = Join-Path $WorkspacePath "runtime"
$LogDirectory = Join-Path $WorkspacePath "logs"
$StatePath = Join-Path $RuntimeDirectory "contextkit-service.json"
$BindAddress = "127.0.0.1"
$Port = 8765
$ServiceUrl = "http://${BindAddress}:${Port}"
$HealthUrl = "${ServiceUrl}/api/health"
$script:LastErrorMessage = $null

function Write-Title {
    Write-Host ""
    Write-Host "知帧 ContextKit 服务管理" -ForegroundColor Cyan
    Write-Host "────────────────────────" -ForegroundColor DarkGray
}

function Write-Ok([string]$Message) {
    Write-Host "✓ $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "! $Message" -ForegroundColor Yellow
}

function Write-Fail([string]$Message) {
    $script:LastErrorMessage = $Message
    Write-Host "× $Message" -ForegroundColor Red
}

function Initialize-ServiceDirectories {
    foreach ($directory in @($WorkspacePath, $RuntimeDirectory, $LogDirectory)) {
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            $null = New-Item -ItemType Directory -Path $directory -Force
        }
    }
}

function Get-ContextKitHealth {
    try {
        return Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 3
    } catch {
        return $null
    }
}

function Test-ContextKitHealth([object]$Health) {
    if ($null -eq $Health) {
        return $false
    }

    $propertyNames = @($Health.PSObject.Properties.Name)
    return (
        $propertyNames -contains "status" -and
        $propertyNames -contains "local_only" -and
        $propertyNames -contains "resumable_uploads" -and
        ($Health.status -eq "ready" -or $Health.status -eq "limited") -and
        $Health.local_only -eq $true -and
        $Health.resumable_uploads -eq $true
    )
}

function Get-ListeningProcessId {
    try {
        $lines = & netstat.exe -ano -p tcp 2>$null
        foreach ($line in $lines) {
            if ($line -match "^\s*TCP\s+(?:127\.0\.0\.1|0\.0\.0\.0):$Port\s+\S+\s+\S+\s+(\d+)\s*$") {
                return [int]$Matches[1]
            }
        }
    } catch {
        # Fall back to the Windows networking cmdlet below.
    }

    try {
        $connection = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
            Where-Object { $_.LocalAddress -eq $BindAddress -or $_.LocalAddress -eq "0.0.0.0" } |
            Select-Object -First 1
        if ($null -ne $connection) {
            return [int]$connection.OwningProcess
        }
    } catch {
        return $null
    }

    return $null
}

function Test-ProcessAlive([int]$ProcessId) {
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Read-ServiceState {
    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return $null
    }

    try {
        return Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Write-ServiceState(
    [int]$ProcessId,
    [string]$StandardOutputLog,
    [string]$StandardErrorLog
) {
    Initialize-ServiceDirectories
    $temporaryStatePath = "$StatePath.tmp"
    $state = [ordered]@{
        pid = $ProcessId
        project_root = $ProjectRoot
        url = $ServiceUrl
        started_at = (Get-Date).ToString("o")
        stdout_log = $StandardOutputLog
        stderr_log = $StandardErrorLog
    }
    $state | ConvertTo-Json | Set-Content -LiteralPath $temporaryStatePath -Encoding UTF8
    Move-Item -Force -LiteralPath $temporaryStatePath -Destination $StatePath
}

function Remove-ServiceState {
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        Remove-Item -Force -LiteralPath $StatePath
    }
}

function Get-StateProcessId {
    $state = Read-ServiceState
    if ($null -eq $state) {
        return $null
    }

    [int]$storedProcessId = 0
    if (-not [int]::TryParse([string]$state.pid, [ref]$storedProcessId)) {
        return $null
    }
    return $storedProcessId
}

function Ensure-ServiceState([int]$ProcessId) {
    $state = Read-ServiceState
    [int]$storedProcessId = 0
    if (
        $null -ne $state -and
        [int]::TryParse([string]$state.pid, [ref]$storedProcessId) -and
        $storedProcessId -eq $ProcessId
    ) {
        return
    }

    $stdoutLog = if ($null -ne $state) { [string]$state.stdout_log } else { "" }
    $stderrLog = if ($null -ne $state) { [string]$state.stderr_log } else { "" }
    Write-ServiceState $ProcessId $stdoutLog $stderrLog
}

function Get-LastLogPaths {
    $state = Read-ServiceState
    if ($null -ne $state -and $state.stdout_log -and $state.stderr_log) {
        return @([string]$state.stdout_log, [string]$state.stderr_log)
    }

    if (-not (Test-Path -LiteralPath $LogDirectory -PathType Container)) {
        return @()
    }

    return @(
        Get-ChildItem -LiteralPath $LogDirectory -File -Filter "contextkit-*.log" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 2 -ExpandProperty FullName
    )
}

function Show-FailureLog([string]$ErrorLogPath) {
    if (-not (Test-Path -LiteralPath $ErrorLogPath -PathType Leaf)) {
        return
    }

    $recentLines = @(Get-Content -LiteralPath $ErrorLogPath -Tail 12 -ErrorAction SilentlyContinue)
    if ($recentLines.Count -gt 0) {
        Write-Host ""
        Write-Host "最近的错误信息：" -ForegroundColor Yellow
        $recentLines | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    }
}

function Install-EnvironmentIfNeeded {
    if (Test-Path -LiteralPath $PythonPath -PathType Leaf) {
        return $true
    }

    if (-not (Test-Path -LiteralPath $SetupScriptPath -PathType Leaf)) {
        Write-Fail "找不到 setup.ps1，无法创建运行环境。"
        return $false
    }

    Write-Warn "首次运行需要安装依赖，完成时间取决于网络速度。"
    try {
        & $SetupScriptPath | Out-Host
    } catch {
        Write-Fail "运行环境安装失败：$($_.Exception.Message)"
        return $false
    }

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        Write-Fail "安装结束后仍未找到项目运行环境。"
        return $false
    }
    return $true
}

function Normalize-ProcessPathVariable {
    # Windows PowerShell 5.1 can fail to start a child process when the current
    # process uses "PATH" while Windows stores the same variable as "Path".
    $processPathValue = [Environment]::GetEnvironmentVariable("PATH", "Process")
    if ([string]::IsNullOrWhiteSpace($processPathValue)) {
        return
    }
    [Environment]::SetEnvironmentVariable("PATH", $null, "Process")
    [Environment]::SetEnvironmentVariable("Path", $processPathValue, "Process")
}

function Open-BrowserSafe {
    try {
        Start-Process $ServiceUrl
        return $true
    } catch {
        Write-Warn "服务已运行，但系统未允许自动打开浏览器。请手动访问 $ServiceUrl"
        return $false
    }
}

function Start-ContextKit([bool]$ShouldOpenBrowser = $false) {
    $script:LastErrorMessage = $null
    Initialize-ServiceDirectories

    $health = Get-ContextKitHealth
    $listenerProcessId = Get-ListeningProcessId
    if (Test-ContextKitHealth $health) {
        if ($null -ne $listenerProcessId) {
            Ensure-ServiceState $listenerProcessId
        }
        Write-Ok "服务已经在运行：$ServiceUrl"
        if ($ShouldOpenBrowser) {
            $null = Open-BrowserSafe
        }
        return $true
    }

    if ($null -ne $listenerProcessId) {
        Write-Fail "端口 $Port 已被其他程序占用。为避免误关程序，知帧没有继续启动。"
        return $false
    }

    if (-not (Install-EnvironmentIfNeeded)) {
        return $false
    }
    if (-not (Test-Path -LiteralPath $RunScriptPath -PathType Leaf)) {
        Write-Fail "找不到 run.py，无法启动服务。"
        return $false
    }

    $timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $standardOutputLog = Join-Path $LogDirectory "contextkit-$timestamp.out.log"
    $standardErrorLog = Join-Path $LogDirectory "contextkit-$timestamp.err.log"
    Write-Host "正在启动，请稍候……" -ForegroundColor Gray

    try {
        Normalize-ProcessPathVariable
        $startParameters = @{
            FilePath = $PythonPath
            ArgumentList = @("run.py", "--host", $BindAddress, "--port", [string]$Port)
            WorkingDirectory = $ProjectRoot
            RedirectStandardOutput = $standardOutputLog
            RedirectStandardError = $standardErrorLog
            WindowStyle = "Hidden"
            PassThru = $true
        }
        $launcher = Start-Process @startParameters
    } catch {
        Write-Fail "启动失败：$($_.Exception.Message)"
        Show-FailureLog $standardErrorLog
        return $false
    }

    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        $health = Get-ContextKitHealth
        $listenerProcessId = Get-ListeningProcessId
        if ((Test-ContextKitHealth $health) -and $null -ne $listenerProcessId) {
            Write-ServiceState $listenerProcessId $standardOutputLog $standardErrorLog
            Write-Ok "启动成功：$ServiceUrl"
            if ($null -ne $health.gpu -and $health.gpu.available -eq $true) {
                Write-Host "  GPU：$($health.gpu.name)（$($health.gpu.recommended_compute_type)）" -ForegroundColor DarkGray
            }
            Write-Host "  日志：$LogDirectory" -ForegroundColor DarkGray
            if ($ShouldOpenBrowser) {
                $null = Open-BrowserSafe
            }
            return $true
        }
        if ($launcher.HasExited) {
            break
        }
    } while ((Get-Date) -lt $deadline)

    Write-Fail "服务未能在 30 秒内正常启动。"
    Show-FailureLog $standardErrorLog
    return $false
}

function Stop-ContextKit([bool]$QuietIfStopped = $false) {
    $script:LastErrorMessage = $null
    $health = Get-ContextKitHealth
    $listenerProcessId = Get-ListeningProcessId
    $stateProcessId = Get-StateProcessId

    if ($null -eq $listenerProcessId) {
        Remove-ServiceState
        if (-not $QuietIfStopped) {
            Write-Ok "服务当前已停止。"
        }
        return $true
    }

    $isRecognizedService = Test-ContextKitHealth $health
    $isRecordedProcess = $null -ne $stateProcessId -and $stateProcessId -eq $listenerProcessId
    if (-not $isRecognizedService -and -not $isRecordedProcess) {
        Write-Fail "端口 $Port 上的程序无法确认为知帧服务，因此没有执行停止操作。"
        return $false
    }

    Write-Host "正在停止服务（进程 $listenerProcessId）……" -ForegroundColor Gray
    try {
        Stop-Process -Id $listenerProcessId -Force -ErrorAction Stop
    } catch {
        Write-Warn "常规停止未成功，正在关闭已确认的知帧进程。"
        $null = & taskkill.exe /PID $listenerProcessId /T /F 2>&1
    }

    $deadline = (Get-Date).AddSeconds(12)
    do {
        Start-Sleep -Milliseconds 400
        if (-not (Test-ProcessAlive $listenerProcessId) -and $null -eq (Get-ListeningProcessId)) {
            Remove-ServiceState
            Write-Ok "服务已停止。"
            return $true
        }
    } while ((Get-Date) -lt $deadline)

    $null = & taskkill.exe /PID $listenerProcessId /T /F 2>&1
    Start-Sleep -Milliseconds 700
    if ($null -eq (Get-ListeningProcessId)) {
        Remove-ServiceState
        Write-Ok "服务已停止。"
        return $true
    }

    Write-Fail "服务仍在占用端口 $Port，请查看日志或重新启动电脑后再试。"
    return $false
}

function Restart-ContextKit([bool]$ShouldOpenBrowser = $false) {
    if (-not (Stop-ContextKit $true)) {
        return $false
    }
    return Start-ContextKit $ShouldOpenBrowser
}

function Show-ContextKitStatus {
    $health = Get-ContextKitHealth
    $listenerProcessId = Get-ListeningProcessId
    if (Test-ContextKitHealth $health) {
        if ($null -ne $listenerProcessId) {
            Ensure-ServiceState $listenerProcessId
        }
        Write-Ok "正在运行"
        Write-Host "  地址：$ServiceUrl" -ForegroundColor Gray
        if ($null -ne $listenerProcessId) {
            Write-Host "  进程：$listenerProcessId" -ForegroundColor Gray
        }
        if ($null -ne $health.gpu -and $health.gpu.available -eq $true) {
            $freeMemory = [math]::Round([double]$health.gpu.memory_free_mb / 1024, 1)
            $totalMemory = [math]::Round([double]$health.gpu.memory_total_mb / 1024, 1)
            Write-Host "  GPU：$($health.gpu.name)，可用 $freeMemory / $totalMemory GB" -ForegroundColor Gray
        } else {
            Write-Host "  GPU：当前不可用，语音模型会回退到 CPU" -ForegroundColor Yellow
        }
        Write-Host "  大文件：支持分片和断点续传" -ForegroundColor Gray
        Write-Host "  日志：$LogDirectory" -ForegroundColor Gray
        return $true
    }

    if ($null -ne $listenerProcessId) {
        Write-Warn "端口 $Port 已被其他程序占用，但它不是可识别的知帧服务。"
        return $false
    }

    Remove-ServiceState
    Write-Warn "服务当前未运行。"
    return $true
}

function Open-ContextKit {
    if (-not (Test-ContextKitHealth (Get-ContextKitHealth))) {
        Write-Warn "服务尚未运行，正在先为你启动。"
        return Start-ContextKit $true
    }
    if (Open-BrowserSafe) {
        Write-Ok "已在浏览器打开知帧。"
        return $true
    }
    return $false
}

function Open-ContextKitLogs {
    Initialize-ServiceDirectories
    try {
        Start-Process explorer.exe -ArgumentList @($LogDirectory)
        Write-Ok "已打开日志文件夹。"
        return $true
    } catch {
        Write-Warn "系统未允许自动打开文件夹。日志位置：$LogDirectory"
        return $false
    }
}

function Invoke-ManagerAction([string]$RequestedAction, [bool]$ShouldOpenBrowser = $false) {
    switch ($RequestedAction) {
        "start" { return Start-ContextKit $ShouldOpenBrowser }
        "stop" { return Stop-ContextKit $false }
        "restart" { return Restart-ContextKit $ShouldOpenBrowser }
        "status" { return Show-ContextKitStatus }
        "open" { return Open-ContextKit }
        "logs" { return Open-ContextKitLogs }
        default { return $false }
    }
}

function Show-ManagerMenu {
    do {
        Clear-Host
        Write-Title
        $null = Show-ContextKitStatus
        Write-Host ""
        Write-Host "  1  启动并打开网页"
        Write-Host "  2  重启服务"
        Write-Host "  3  停止服务"
        Write-Host "  4  刷新运行状态"
        Write-Host "  5  打开网页"
        Write-Host "  6  打开日志文件夹"
        Write-Host "  0  退出"
        Write-Host ""
        $choice = Read-Host "请选择"

        $performedAction = $true
        switch ($choice) {
            "1" { $null = Start-ContextKit $true }
            "2" { $null = Restart-ContextKit $false }
            "3" { $null = Stop-ContextKit $false }
            "4" { $null = Show-ContextKitStatus }
            "5" { $null = Open-ContextKit }
            "6" { $null = Open-ContextKitLogs }
            "0" { return }
            default {
                Write-Warn "请输入 0～6。"
            }
        }

        if ($performedAction) {
            Write-Host ""
            $null = Read-Host "按回车键返回菜单"
        }
    } while ($true)
}

Set-Location -LiteralPath $ProjectRoot
if ($Action -eq "menu") {
    Show-ManagerMenu
    exit 0
}

Write-Title
$success = Invoke-ManagerAction $Action $OpenBrowser.IsPresent
if ($success) {
    exit 0
}
exit 1

