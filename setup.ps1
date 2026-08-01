<#
.SYNOPSIS
    One-command setup for UltimateScrape on Windows.

.DESCRIPTION
    Installs uv if missing, creates the virtual environment, installs the package,
    writes a starter .env, and runs a health check. Safe to re-run — every step is
    idempotent.

.EXAMPLE
    .\setup.ps1
    .\setup.ps1 -WithBrowser        # also install the headless-browser tier
#>
[CmdletBinding()]
param(
    [switch]$WithBrowser,
    [string]$Python = "3.13"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $repo

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "  OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  !   $msg" -ForegroundColor Yellow }

Write-Step "Checking Python"
$pythonOk = $false
foreach ($cmd in @("python", "py")) {
    try {
        $v = & $cmd --version 2>&1
        if ($v -match "Python 3\.(1[1-9]|[2-9][0-9])") { Write-Ok "$v"; $pythonOk = $true; break }
    } catch { }
}
if (-not $pythonOk) {
    Write-Warn2 "Python 3.11+ not found."
    Write-Host "  Install it from https://www.python.org/downloads/ or run:  winget install Python.Python.3.13"
    Write-Host "  IMPORTANT: tick 'Add python.exe to PATH' in the installer."
    exit 1
}

Write-Step "Checking uv"
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "  installing uv..."
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    # uv installs to %USERPROFILE%\.local\bin, which is not on PATH until a new shell.
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Warn2 "uv installed but not on PATH. Close and reopen PowerShell, then re-run this script."
    exit 1
}
Write-Ok (uv --version)

Write-Step "Creating the virtual environment"
if (-not (Test-Path ".venv")) { uv venv --python $Python } else { Write-Ok ".venv already exists" }

Write-Step "Installing UltimateScrape"
uv pip install -e ".[dev,export]" --quiet
Write-Ok "installed (core + spreadsheet export)"

if ($WithBrowser) {
    Write-Step "Installing the headless-browser tier"
    uv pip install -e ".[crawl]" --quiet
    & ".\.venv\Scripts\crawl4ai-setup.exe"
    Write-Ok "crawl4ai ready"
}

Write-Step "Configuration"
if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Ok "created .env from the template"
    Write-Warn2 "Open .env and set OPENROUTER_API_KEY before running any research command."
} else {
    Write-Ok ".env already exists — leaving it alone"
}

Write-Step "Health check"
& ".\.venv\Scripts\uscrape.exe" doctor

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host @"

Everything runs through the venv. Either prefix commands:

    .\.venv\Scripts\uscrape.exe doctor

or activate the environment once per terminal and drop the prefix:

    .\.venv\Scripts\Activate.ps1
    uscrape doctor

If activation is blocked by execution policy, run this once:

    Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser

Try these — neither needs an API key:

    uscrape platforms
    uscrape jobs -p imerit -p appen --pay-only

"@ -ForegroundColor Gray
