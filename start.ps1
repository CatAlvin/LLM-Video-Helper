$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "Dependencies are not installed yet. Running setup first..."
    & "$ProjectRoot\setup.ps1"
}

Write-Host ""
Write-Host "ContextKit is starting at http://127.0.0.1:8765"
Write-Host "Press Ctrl+C to stop."
& ".venv\Scripts\python.exe" run.py --host 127.0.0.1 --port 8765
if ($LASTEXITCODE -ne 0) {
    throw "ContextKit stopped because the local service could not start."
}
