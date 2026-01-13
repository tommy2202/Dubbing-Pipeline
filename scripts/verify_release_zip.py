from __future__ import annotations

import argparse
import fnmatch
import sys
import zipfile
from collections import Counter
from pathlib import Path

FORBIDDEN_PATTERNS = [
    "build/**",
    "dist/**",
    "releases/**",
    "backups/**",
    "_tmp*",
    "_tmp*/**",
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.db",
    "**/*.sqlite",
    "**/*.sqlite3",
    "logs/**",
    "**/*.log",
]


def _norm(name: str) -> str:
    return (name or "").replace("\\", "/").lstrip("/")


def _is_forbidden(member: str) -> bool:
    m = _norm(member)
    if not m or m.endswith("/"):
        return False
    # Input/Output are forbidden except placeholders
    if m.startswith("Input/") and m != "Input/.gitkeep":
        return True
    if m.startswith("Output/") and m != "Output/.gitkeep":
        return True
    return any(fnmatch.fnmatch(m, pat) for pat in FORBIDDEN_PATTERNS)


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a release zip contains no runtime artifacts.")
    ap.add_argument("zip", help="Path to the zip file to verify.")
    args = ap.parse_args()

    zp = Path(args.zip).expanduser().resolve()
    if not zp.exists():
        print(f"Zip not found: {zp}", file=sys.stderr)
        return 2

    offenders: list[str] = []
    files: list[str] = []
    total_uncompressed = 0

    with zipfile.ZipFile(zp) as zf:
        for info in zf.infolist():
            name = _norm(info.filename)
            if not name or name.endswith("/"):
                continue
            files.append(name)
            total_uncompressed += int(getattr(info, "file_size", 0) or 0)
            if _is_forbidden(name):
                offenders.append(name)

    # Summary
    tops = Counter(
        [f.split("/", 1)[0] for f in files if "/" in f] + [f for f in files if "/" not in f]
    )
    print(f"Zip: {zp}")
    print(f"Entries: {len(files)}")
    print(f"Total uncompressed bytes: {total_uncompressed}")
    print("Top-level contents (counts):")
    for k, v in tops.most_common(50):
        print(f"- {k}: {v}")

    if offenders:
        print("\nERROR: forbidden paths detected in release zip:", file=sys.stderr)
        for o in sorted(set(offenders)):
            print(f"- {o}", file=sys.stderr)
        return 1

    print("\nverify_release_zip: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
