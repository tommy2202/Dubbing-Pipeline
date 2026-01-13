from __future__ import annotations

import os
from pathlib import Path


class UnsafeRuntimeDbPath(RuntimeError):
    pass


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except Exception:
        return False


def assert_safe_runtime_db_path(
    db_path: Path,
    *,
    purpose: str,
    repo_root: Path | None,
    allowed_repo_subdirs: list[Path],
) -> None:
    """
    Guardrail: refuse storing sensitive runtime DBs in unsafe repo locations.

    Rules:
    - Always forbid obvious artifact folders: build/, dist/, backups/, and any _tmp* directory.
    - If inside a git repo workspace, only allow DBs under an explicit runtime-only subdir
      (e.g. Output/_state/) rather than repo root or arbitrary tracked locations.
    """
    p = Path(db_path).expanduser()
    try:
        p = p.resolve()
    except Exception:
        # Best-effort: still validate on the normalized path.
        p = p.absolute()

    # Reject dangerous directories by path component.
    parts = [x for x in p.parts if x]
    for comp in parts:
        c = str(comp)
        if c in {"build", "dist", "backups"}:
            raise UnsafeRuntimeDbPath(f"Refusing to store {purpose} DB in '{c}/' (unsafe): {p}")
        if c.startswith("_tmp"):
            raise UnsafeRuntimeDbPath(f"Refusing to store {purpose} DB in '_tmp*' (unsafe): {p}")

    if repo_root is None:
        return

    rr = Path(repo_root).expanduser()
    try:
        rr = rr.resolve()
    except Exception:
        rr = rr.absolute()

    if not _is_relative_to(p, rr):
        # DB outside repo: OK (recommended for production).
        return

    # Inside repo: must be inside an explicitly-allowed runtime-only subdir.
    allowed = []
    for a in allowed_repo_subdirs:
        try:
            ap = Path(a).expanduser().resolve()
        except Exception:
            ap = Path(a).expanduser().absolute()
        allowed.append(ap)

    if not any(_is_relative_to(p, ap) for ap in allowed):
        raise UnsafeRuntimeDbPath(
            f"Refusing to store {purpose} DB inside repo workspace outside allowed runtime dirs. "
            f"db_path={p} repo_root={rr} allowed={[str(x) for x in allowed]}"
        )

    # Optional: if git is present and the file already exists, fail hard if tracked.
    # This catches accidental commits or release workspaces that include DBs.
    if p.exists():
        try:
            import subprocess

            if (rr / ".git").exists() and os.environ.get("SKIP_GIT_TRACK_CHECK", "").strip() != "1":
                subprocess.check_output(
                    ["git", "-C", str(rr), "ls-files", "--error-unmatch", str(p.relative_to(rr))],
                    stderr=subprocess.DEVNULL,
                )
                raise UnsafeRuntimeDbPath(
                    f"{purpose} DB appears to be git-tracked (must never be tracked): {p}"
                )
        except subprocess.CalledProcessError:
            pass
        except Exception:
            # Never block boot purely because git isn't available.
            pass
