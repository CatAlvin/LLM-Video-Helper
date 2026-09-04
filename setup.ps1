$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath ".venv\Scripts\python.exe")) {
    Write-Host "[1/3] Creating a private Python environment..."
    python -m venv .venv
}

Write-Host "[2/3] Updating the package installer..."
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) {
    throw "Could not update pip. Check the network connection and try again."
}

Write-Host "[3/3] Installing media and document processors..."
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    throw "Could not install dependencies. Check the messages above and try again."
}

Write-Host ""
Write-Host "Setup complete. Run .\start.ps1 to launch ContextKit."
