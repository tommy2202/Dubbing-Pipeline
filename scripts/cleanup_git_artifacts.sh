#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/cleanup_git_artifacts.sh [--apply] [--yes]

Untracks (git rm --cached) any currently-tracked runtime/build artifacts
that should not live in source control. This script does NOT delete files
from disk; it only updates the git index.

Modes:
  (default) Dry run: show what would be untracked
  --apply            Actually untrack the files

Safety:
  --yes              Skip confirmation prompt (required for non-interactive runs)

Examples:
  scripts/cleanup_git_artifacts.sh
  scripts/cleanup_git_artifacts.sh --apply
  scripts/cleanup_git_artifacts.sh --apply --yes
EOF
}

APPLY=0
YES=0

for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --yes) YES=1 ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $arg" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v git >/dev/null 2>&1; then
  echo "git is required" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

paths_file="$(mktemp)"
trap 'rm -f "$paths_file"' EXIT

python3 - <<'PY' >"$paths_file"
import re, subprocess, sys

tracked = subprocess.check_output(["git", "ls-files"], text=True).splitlines()

patterns = [
    r"(^|/)__pycache__/",
    r"\.pyc$",
    r"\.pyo$",
    r"^build/",
    r"^dist/",
    r"\.egg-info/",
    r"^Output/",
    r"^Input/",
    r"^backups/",
    r"^logs/",
    r"\.log$",
    r"^_tmp",      # _tmp*, _tmp_*, etc.
    r"^tmp/",
    r"\.db$",
]

rx = re.compile("|".join(f"(?:{p})" for p in patterns))

deny = set()
for p in tracked:
    if rx.search(p):
        deny.add(p)

# Keep placeholders, even though Input/Output are ignored.
deny.discard("Input/.gitkeep")
deny.discard("Output/.gitkeep")

for p in sorted(deny):
    sys.stdout.write(p + "\n")
PY

count="$(wc -l <"$paths_file" | tr -d ' ')"
echo "Tracked artifact paths detected: $count"
echo

if [[ "$count" == "0" ]]; then
  echo "Nothing to do."
  exit 0
fi

sed -n '1,200p' "$paths_file"
if [[ "$count" -gt 200 ]]; then
  echo "... (truncated; showing first 200)"
fi
echo

if [[ "$APPLY" -eq 0 ]]; then
  echo "Dry run mode: no changes made."
  echo "Re-run with --apply to untrack these files."
  exit 0
fi

if [[ "$YES" -eq 0 ]]; then
  if [[ ! -t 0 ]]; then
    echo "Non-interactive session: pass --yes to proceed." >&2
    exit 2
  fi
  echo "About to run: git rm -r --cached <listed paths>"
  read -r -p "Type 'yes' to proceed: " confirm
  if [[ "$confirm" != "yes" ]]; then
    echo "Aborted."
    exit 1
  fi
fi

# Ensure placeholder dirs/files exist, so we can re-add them after untracking.
mkdir -p Input Output
touch Input/.gitkeep Output/.gitkeep

null_paths="$(mktemp)"
trap 'rm -f "$paths_file" "$null_paths"' EXIT

python3 - <<'PY' "$paths_file" "$null_paths"
import sys
src, dst = sys.argv[1], sys.argv[2]
with open(src, "r", encoding="utf-8") as f_in, open(dst, "wb") as f_out:
    for line in f_in:
        p = line.rstrip("\n")
        if not p:
            continue
        f_out.write(p.encode("utf-8") + b"\0")
PY

git rm -r --cached --pathspec-from-file="$null_paths" --pathspec-file-nul

# Re-add placeholders (force because they are ignored).
git add -f Input/.gitkeep Output/.gitkeep

echo
echo "Done. Review with: git status"

