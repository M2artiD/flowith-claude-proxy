# Flowith Claude Proxy — Windows one-click launcher
# Usage: .\start.ps1 [--port 8787] [--host 127.0.0.1]

param(
    [string]$HostAddr = "127.0.0.1",
    [string]$Port = "8787"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectRoot

# 1. Check .env
if (-not (Test-Path ".env")) {
    Write-Host "[!] .env not found, copying from .env.example ..." -ForegroundColor Yellow
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "[!] Please edit .env and set FLOWITH_API_KEY, then re-run." -ForegroundColor Yellow
    } else {
        Write-Host "[!] Please create .env with FLOWITH_API_KEY=your_key" -ForegroundColor Yellow
    }
    exit 1
}

# 2. Activate venv if present
if (Test-Path ".venv\Scripts\Activate.ps1") {
    Write-Host "[*] Activating virtualenv ..." -ForegroundColor Cyan
    . .\.venv\Scripts\Activate.ps1
}

# 3. Ensure the package is installed
python -c "import flowith_claude_proxy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[*] Installing package (first run) ..." -ForegroundColor Cyan
    pip install -q -e .
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[!] pip install failed. Check your Python / pip setup." -ForegroundColor Red
        exit 1
    }
}

# 4. Launch
Write-Host ""
python -m flowith_claude_proxy --host $HostAddr --port $Port
