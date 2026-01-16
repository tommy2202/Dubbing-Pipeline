from __future__ import annotations

import argparse
import fnmatch
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PackageSpec:
    include_dirs: list[str]
    include_files: list[str]
    include_optional_files: list[str]
    include_optional_dirs: list[str]


FORBIDDEN_DIR_PREFIXES = (
    "build/",
    "dist/",
    "backups/",
    "_tmp",
    "__pycache__/",
    "logs/",
)

FORBIDDEN_GLOBS = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.db",
    "**/*.sqlite",
    "**/*.sqlite3",
    "**/*.log",
)


def repo_root() -> Path:
    # scripts/ -> repo root
    return Path(__file__).resolve().parents[1]


def norm_rel(p: Path, root: Path) -> str:
    rel = p.resolve().relative_to(root.resolve())
    return str(rel).replace("\\", "/")


def is_forbidden_rel(rel: str) -> bool:
    rel = rel.lstrip("/").replace("\\", "/")

    # Never package runtime folders, except placeholders.
    if rel.startswith("Input/") and rel != "Input/.gitkeep":
        return True
    if rel.startswith("Output/") and rel != "Output/.gitkeep":
        return True

    # Obvious runtime/build dirs
    for pref in FORBIDDEN_DIR_PREFIXES:
        if pref.endswith("/") and rel.startswith(pref):
            return True
        if pref == "_tmp" and rel.startswith("_tmp"):
            return True

    # Explicit forbidden file globs (in any allowed dir)
    return any(fnmatch.fnmatch(rel, pat) for pat in FORBIDDEN_GLOBS)


def iter_included_files(root: Path, spec: PackageSpec) -> list[Path]:
    out: list[Path] = []

    # Mandatory dirs
    for d in spec.include_dirs:
        dp = (root / d).resolve()
        if not dp.exists():
            raise FileNotFoundError(f"Required directory missing: {d}")
        for p in dp.rglob("*"):
            if not p.is_file():
                continue
            rel = norm_rel(p, root)
            if is_forbidden_rel(rel):
                continue
            out.append(p)

    # Optional dirs (include if present)
    for d in spec.include_optional_dirs:
        dp = (root / d).resolve()
        if not dp.exists():
            continue
        for p in dp.rglob("*"):
            if not p.is_file():
                continue
            rel = norm_rel(p, root)
            if is_forbidden_rel(rel):
                continue
            out.append(p)

    # Mandatory files
    for f in spec.include_files:
        fp = (root / f).resolve()
        if not fp.exists():
            raise FileNotFoundError(f"Required file missing: {f}")
        rel = norm_rel(fp, root)
        if is_forbidden_rel(rel):
            raise RuntimeError(f"Allowlisted file is forbidden by policy: {rel}")
        out.append(fp)

    # Optional files
    for f in spec.include_optional_files:
        fp = (root / f).resolve()
        if not fp.exists():
            continue
        rel = norm_rel(fp, root)
        if is_forbidden_rel(rel):
            continue
        out.append(fp)

    # Explicitly include keep markers (even though Input/Output are excluded).
    for keep_path in ("Input/.gitkeep", "Output/.gitkeep"):
        fp = (root / keep_path).resolve()
        if fp.exists():
            out.append(fp)

    # De-dupe and stable order
    uniq = {p.resolve(): p for p in out}
    return [uniq[k] for k in sorted(uniq.keys(), key=lambda x: str(x))]


def build_zip(
    *,
    root: Path,
    out_dir: Path,
    name: str,
    files: list[Path],
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = (out_dir / name).resolve()

    # If writing to dist/, don't accidentally include the zip itself during the walk.
    if zip_path.exists():
        zip_path.unlink()

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for p in files:
            rel = norm_rel(p, root)
            if rel.startswith("dist/") or rel.startswith("releases/"):
                # Never include previous release outputs
                continue
            if is_forbidden_rel(rel):
                continue
            zf.write(p, arcname=rel)

    return zip_path


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a safe, allowlist-based release zip.")
    ap.add_argument(
        "--out",
        default="dist",
        choices=["dist", "releases"],
        help="Output directory (created if missing).",
    )
    ap.add_argument(
        "--name",
        default="",
        help="Zip filename. Defaults to dubbing-pipeline-release-YYYYmmdd-HHMMSS.zip",
    )
    args = ap.parse_args()

    root = repo_root()

    spec = PackageSpec(
        include_dirs=[
            "src",
            "config",
            "docs",
            "docker",
            "scripts",
        ],
        include_optional_dirs=[
            "samples",
            "deploy",  # includes compose files; filtered by forbidden patterns
        ],
        include_files=[
            "pyproject.toml",
            "README.md",
            ".env.example",
            ".env.secrets.example",
        ],
        include_optional_files=[
            "README-deploy.md",
            "main.py",
        ],
    )

    files = iter_included_files(root, spec)

    # Enforce "samples small only" policy: skip any sample over 5MB.
    # (Rule says "small, non-sensitive files only"; this is an offline safety guard.)
    filtered: list[Path] = []
    for p in files:
        rel = norm_rel(p, root)
        if rel.startswith("samples/"):
            try:
                if p.stat().st_size > 5 * 1024 * 1024:
                    continue
            except Exception:
                continue
        filtered.append(p)
    files = filtered

    ts = time.strftime("%Y%m%d-%H%M%S")
    name = args.name.strip() or f"dubbing-pipeline-release-{ts}.zip"
    if not name.endswith(".zip"):
        name += ".zip"

    out_dir = (root / args.out).resolve()
    zip_path = build_zip(root=root, out_dir=out_dir, name=name, files=files)

    total_bytes = zip_path.stat().st_size
    print(f"Wrote release zip: {zip_path} ({total_bytes} bytes)")
    print(f"Files included: {len(files)}")
    return 0


if __name__ == "__main__":
    # Ensure packaging does not depend on PATH-modified tools.
    os.environ.setdefault("PYTHONUTF8", "1")
    raise SystemExit(main())
