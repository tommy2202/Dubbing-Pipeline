from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path


def _repo_root() -> Path:
    # scripts/ -> repo root
    return Path(__file__).resolve().parents[1]


def _iter_zip_paths(root: Path) -> list[Path]:
    zips: list[Path] = []
    for p in root.rglob("*.zip"):
        # Skip git internals
        if ".git" in p.parts:
            continue
        zips.append(p)
    return zips


def _is_forbidden_location(path: Path, *, root: Path) -> bool:
    """
    Forbidden if:
    - path is in repo root (direct child), OR
    - path is anywhere under build/, dist/, backups/, OR
    - path is anywhere under a _tmp* directory.
    """
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        return False

    parts = rel.parts
    if not parts:
        return False

    # Directly under repo root (e.g. ./auth.db)
    if len(parts) == 1:
        return True

    top = parts[0]
    if top in {"build", "dist", "backups"}:
        return True

    for comp in parts:
        if comp.startswith("_tmp"):
            return True

    return False


def main() -> int:
    root = _repo_root()

    auth_db_name = (os.environ.get("ANIME_V2_AUTH_DB_NAME") or "auth.db").strip()
    jobs_db_name = (os.environ.get("ANIME_V2_JOBS_DB_NAME") or "jobs.db").strip()
    names = {auth_db_name, jobs_db_name}

    offenders: list[str] = []

    # 1) Fail if these DBs exist in forbidden locations in the workspace.
    for name in sorted(names):
        for p in root.rglob(name):
            if ".git" in p.parts:
                continue
            if _is_forbidden_location(p, root=root):
                offenders.append(f"forbidden_db_path:{p}")

    # 2) Fail if any *.db appears under build/, dist/, backups/, or _tmp* (broad safety net).
    for p in root.rglob("*.db"):
        if ".git" in p.parts:
            continue
        if _is_forbidden_location(p, root=root):
            offenders.append(f"forbidden_any_db_path:{p}")

    # 3) Fail if any zip file contains auth.db / jobs.db or any *.db member.
    for z in _iter_zip_paths(root):
        try:
            with zipfile.ZipFile(z) as zf:
                for info in zf.infolist():
                    n = (info.filename or "").replace("\\", "/").rstrip("/")
                    if not n:
                        continue
                    base = n.split("/")[-1]
                    if base in names or base.endswith(".db") or base.endswith(".sqlite") or base.endswith(".sqlite3"):
                        offenders.append(f"zip_contains_db:{z}:{n}")
        except zipfile.BadZipFile:
            # If it's not a valid zip, ignore here (other tooling should catch).
            continue

    # 4) Fail if any of these DB names are tracked by git.
    try:
        import subprocess

        tracked = set(
            subprocess.check_output(["git", "-C", str(root), "ls-files"], text=True).splitlines()
        )
        for t in sorted(tracked):
            if t.endswith(".db") or t.endswith(".sqlite") or t.endswith(".sqlite3"):
                offenders.append(f"git_tracked_db:{t}")
            # Explicit names anywhere
            if t.endswith("/" + auth_db_name) or t == auth_db_name:
                offenders.append(f"git_tracked_auth_db:{t}")
    except Exception:
        # Do not fail purely because git is unavailable.
        pass

    if offenders:
        print("ERROR: Sensitive runtime DB files detected in forbidden locations.", file=sys.stderr)
        for o in sorted(set(offenders)):
            print(f"- {o}", file=sys.stderr)
        print(
            "Fix: ensure runtime DBs live under Output/_state (or ANIME_V2_STATE_DIR) and are never committed or bundled.",
            file=sys.stderr,
        )
        return 1

    print("check_no_sensitive_runtime_files: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

