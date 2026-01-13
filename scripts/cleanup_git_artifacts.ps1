Param(
  [switch]$Apply,
  [switch]$Yes,
  [switch]$Help
)

if ($Help) {
  @"
Usage: scripts/cleanup_git_artifacts.ps1 [-Apply] [-Yes]

Untracks (git rm --cached) any currently-tracked runtime/build artifacts
that should not live in source control. This script does NOT delete files
from disk; it only updates the git index.

Modes:
  (default) Dry run: show what would be untracked
  -Apply            Actually untrack the files

Safety:
  -Yes              Skip confirmation prompt (required for non-interactive runs)

Examples:
  pwsh -File scripts/cleanup_git_artifacts.ps1
  pwsh -File scripts/cleanup_git_artifacts.ps1 -Apply
  pwsh -File scripts/cleanup_git_artifacts.ps1 -Apply -Yes
"@
  exit 0
}

function Require-Command($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    Write-Error "$name is required"
    exit 1
  }
}

Require-Command git

$repoRoot = (git rev-parse --show-toplevel) 2>$null
if (-not $repoRoot) {
  Write-Error "Not inside a git repository."
  exit 1
}

Set-Location $repoRoot

$tracked = (git ls-files) 2>$null
if (-not $tracked) {
  $tracked = @()
}

$patterns = @(
  '(^|/)__pycache__/',
  '\.pyc$',
  '\.pyo$',
  '^build/',
  '^dist/',
  '\.egg-info/',
  '^Output/',
  '^Input/',
  '^backups/',
  '^logs/',
  '\.log$',
  '^_tmp',
  '^tmp/',
  '\.db$'
)

$rx = [regex]::new(($patterns -join '|'))

$paths = $tracked | Where-Object { $rx.IsMatch($_) } | Where-Object {
  $_ -ne 'Input/.gitkeep' -and $_ -ne 'Output/.gitkeep'
} | Sort-Object -Unique

Write-Host ("Tracked artifact paths detected: {0}" -f $paths.Count)
Write-Host ""

if ($paths.Count -eq 0) {
  Write-Host "Nothing to do."
  exit 0
}

$paths | Select-Object -First 200 | ForEach-Object { Write-Host $_ }
if ($paths.Count -gt 200) {
  Write-Host "... (truncated; showing first 200)"
}
Write-Host ""

if (-not $Apply) {
  Write-Host "Dry run mode: no changes made."
  Write-Host "Re-run with -Apply to untrack these files."
  exit 0
}

if (-not $Yes) {
  $confirm = Read-Host "Type 'yes' to proceed with git rm --cached"
  if ($confirm -ne 'yes') {
    Write-Host "Aborted."
    exit 1
  }
}

# Ensure placeholders exist, so we can re-add them after untracking.
New-Item -ItemType Directory -Force -Path "Input" | Out-Null
New-Item -ItemType Directory -Force -Path "Output" | Out-Null
New-Item -ItemType File -Force -Path "Input/.gitkeep" | Out-Null
New-Item -ItemType File -Force -Path "Output/.gitkeep" | Out-Null

foreach ($p in $paths) {
  & git rm -r --cached -- "$p"
  if ($LASTEXITCODE -ne 0) {
    Write-Error "git rm failed for: $p"
    exit 1
  }
}

# Re-add placeholders (force because they are ignored).
& git add -f "Input/.gitkeep" "Output/.gitkeep"
if ($LASTEXITCODE -ne 0) {
  Write-Error "git add failed for .gitkeep files"
  exit 1
}

Write-Host ""
Write-Host "Done. Review with: git status"

