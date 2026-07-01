# start.ps1 — Windows launcher for RAPP Brainstem
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Refresh PATH so newly-installed tools (gh, python) are found
$env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("Path", "User")

# Ensure UTF-8 output from Python
$env:PYTHONUTF8 = "1"

# Resolve a REAL Python 3 (not the Windows Store execution-alias stub, which is a
# valid "command" but only prints "Python was not found" and opens the Store).
$py = $null
foreach ($cmd in @("python", "python3")) {
    try {
        $out = & $cmd --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $out -match "Python 3\.") { $py = $cmd; break }
    } catch {}
}
if (-not $py) {
    Write-Host "ERROR: Python 3 not found on PATH. Install Python 3.11+ from https://python.org" -ForegroundColor Red
    Write-Host "       (Check 'Add Python to PATH' during install.)" -ForegroundColor Yellow
    exit 1
}

# Create .env from the example on first run (parity with start.sh).
if ((-not (Test-Path ".env")) -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
}

# Dependency check. Run under EAP=Continue: at the script's global EAP=Stop, a native
# command writing to stderr (which a missing import does) is promoted to a TERMINATING
# error on Windows PowerShell 5.1 and would abort the launcher before it could install.
function Test-Deps {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $py -c "import flask, flask_cors, requests, dotenv" 2>$null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $prev
    }
}

if (-not (Test-Deps)) {
    Write-Host "Installing dependencies..." -ForegroundColor Yellow
    & $py -m pip install -r requirements.txt -q
    if (-not (Test-Deps)) {
        & $py -m pip install -r requirements.txt
    }
}

# Check gh CLI (optional — the web login flow works without it)
$gh = Get-Command gh -ErrorAction SilentlyContinue
if ($gh) {
    Write-Host "gh CLI found — token will be auto-detected if you're logged in." -ForegroundColor Green
} else {
    Write-Host "gh CLI not found — you can authenticate via the web UI at http://localhost:7071" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Starting RAPP Brainstem..." -ForegroundColor Cyan
& $py brainstem.py
