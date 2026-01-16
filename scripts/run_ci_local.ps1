$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$Py = if ($env:PYTHON) { $env:PYTHON } elseif (Get-Command "python3.10" -ErrorAction SilentlyContinue) { "python3.10" } else { "python" }

Write-Host "Running core CI locally (repo root: $Root)"
Write-Host ("- python: " + (& $Py --version 2>$null))

& $Py -c "import sys; raise SystemExit(0 if (sys.version_info.major, sys.version_info.minor)==(3,10) else 1)" 2>$null
if ($LASTEXITCODE -ne 0) {
  throw "core CI expects Python 3.10. Install python3.10 or set PYTHON to a 3.10 interpreter."
}

function Require-Cmd([string]$Name) {
  $cmd = Get-Command $Name -ErrorAction SilentlyContinue
  if (-not $cmd) {
    throw "Missing required command: $Name"
  }
}

Require-Cmd "ffmpeg"
Require-Cmd "ffprobe"

Write-Host "Installing project + dev deps..."
& $Py -m pip install --upgrade pip
& $Py -m pip install -e ".[dev]"
& $Py -m pip install "openai-whisper==20231117"

Write-Host "Guardrails..."
& $Py scripts/check_no_tracked_artifacts.py
& $Py scripts/check_no_secrets.py

Write-Host "Smoke import all..."
& $Py scripts/smoke_import_all.py

Write-Host "Package + verify release zip..."
New-Item -ItemType Directory -Force -Path "dist" | Out-Null
& $Py scripts/package_release.py --out dist --name local-ci-release.zip
& $Py scripts/verify_release_zip.py dist/local-ci-release.zip

Write-Host "Repo gates..."
& $Py scripts/verify_env.py
& $Py scripts/polish_gate.py
& $Py scripts/mobile_gate.py
& $Py scripts/security_mobile_gate.py
& $Py scripts/security_smoke.py

Write-Host "OK: core CI passed locally"

