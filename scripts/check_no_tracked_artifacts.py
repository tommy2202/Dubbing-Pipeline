from __future__ import annotations

import subprocess
import sys


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def _is_artifact(path: str) -> bool:
    p = path.lstrip("/").replace("\\", "/")

    # Runtime dirs: allowed placeholders only
    if p.startswith("Input/") and p != "Input/.gitkeep":
        return True
    if p.startswith("Output/") and p != "Output/.gitkeep":
        return True

    # Build/release/tmp/backup dirs
    if p.startswith("build/"):
        return True
    if p.startswith("dist/"):
        return True
    if p.startswith("backups/"):
        return True
    if p.startswith("data/reports/"):
        return True
    if p.startswith("voices/embeddings/"):
        return True
    if p.startswith("_tmp") or "/_tmp" in p:
        return True

    # Python caches
    if "/__pycache__/" in f"/{p}/" or p.startswith("__pycache__/"):
        return True
    if p.endswith(".pyc") or p.endswith(".pyo"):
        return True

    # Sensitive runtime files
    if p.endswith(".db"):
        return True
    return p.endswith(".log")


def main() -> int:
    try:
        files = _tracked_files()
    except Exception as ex:
        print(f"ERROR: failed to run git ls-files: {ex}", file=sys.stderr)
        return 2

    offenders = [p for p in files if _is_artifact(p)]

    if offenders:
        print("ERROR: Tracked artifact files detected (must not be committed).", file=sys.stderr)
        for p in offenders:
            print(f"- {p}", file=sys.stderr)
        print(
            "\nFix: remove from git tracking (git rm --cached) and ensure .gitignore covers them.",
            file=sys.stderr,
        )
        return 1

    print("check_no_tracked_artifacts: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
