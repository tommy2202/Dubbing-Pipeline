#!/usr/bin/env python3
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.security.crypto import FORMAT_VERSION_CHUNKED, MAGIC_NEW, MAGIC_OLD
from dubbing_pipeline.stages.character_store import (
    CharacterStore,
    _MAGIC_NEW as CHAR_MAGIC_NEW,
    _MAGIC_OLD as CHAR_MAGIC_OLD,
)


def _read_prefix(path: Path, length: int) -> bytes:
    with path.open("rb") as f:
        return f.read(length)


def _candidate_roots() -> tuple[Path, list[Path]]:
    s = get_settings()
    app_root = Path(s.app_root).resolve()
    roots: list[Path] = []

    def _add(p: object) -> None:
        if not p:
            return
        try:
            rp = Path(str(p)).resolve()
        except Exception:
            return
        if rp not in roots:
            roots.append(rp)

    _add(getattr(s, "output_dir", None))
    _add(getattr(s, "outputs_dir", None))
    _add(getattr(s, "legacy_output_dir", None))
    _add(app_root / "Output")
    _add(app_root / "outputs")
    return app_root, roots


def _iter_files(root: Path) -> list[Path]:
    out: list[Path] = []
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            out.append(p)
    except Exception:
        return out
    return out


def _rewrite_encryption_header(path: Path, *, apply: bool) -> str | None:
    header_len = len(MAGIC_OLD) + 1 + 4
    try:
        head = _read_prefix(path, header_len)
    except Exception:
        return None
    if len(head) < header_len:
        return None
    if head[: len(MAGIC_OLD)] != MAGIC_OLD:
        return None
    ver = int(head[len(MAGIC_OLD)])
    if ver != int(FORMAT_VERSION_CHUNKED):
        return "skip_unknown_format"
    chunk_bytes = struct.unpack(">I", head[len(MAGIC_OLD) + 1 : header_len])[0]
    if int(chunk_bytes) <= 0:
        return "skip_unknown_format"
    if not apply:
        return "would_update"
    try:
        with path.open("r+b") as f:
            f.write(MAGIC_NEW)
        return "updated"
    except Exception:
        return "failed"


def _migrate_character_store(path: Path, *, apply: bool) -> str:
    if not path.exists() or not path.is_file():
        return "missing"
    try:
        head = _read_prefix(path, len(CHAR_MAGIC_NEW))
    except Exception:
        return "skip"
    if head == CHAR_MAGIC_NEW:
        return "already_new"
    if head != CHAR_MAGIC_OLD:
        return "skip"
    if not apply:
        return "would_migrate"
    try:
        store = CharacterStore(path)
        store.load()
        store.save()
        return "migrated"
    except Exception:
        return "failed"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migrate legacy marker headers to new markers (dry-run by default)."
    )
    ap.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    args = ap.parse_args()

    if len(MAGIC_NEW) != len(MAGIC_OLD):
        print("ERROR: magic sizes differ; cannot rewrite in place.")
        return 2

    app_root, roots = _candidate_roots()
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"{mode}: scanning output roots for legacy markers.")

    missing_roots = 0
    for r in roots:
        if not r.exists():
            print(f"SKIP: root missing: {r}")
            missing_roots += 1

    touched = 0
    updated = 0
    skipped_bad_format = 0
    for r in roots:
        if not r.exists() or not r.is_dir():
            continue
        for p in _iter_files(r):
            if p.name == "characters.json":
                continue
            res = _rewrite_encryption_header(p, apply=args.apply)
            if res is None:
                continue
            if res == "skip_unknown_format":
                print(f"WARN: legacy marker but unknown format; skipped: {p}")
                skipped_bad_format += 1
                continue
            if res == "failed":
                print(f"WARN: failed to update marker: {p}")
                continue
            touched += 1
            if res == "updated":
                updated += 1
                print(f"UPDATED: header marker rewritten: {p}")
            elif res == "would_update":
                print(f"DRY-RUN: would update header marker: {p}")

    char_candidates = [
        (app_root / "data" / "characters.json").resolve(),
    ]
    for r in roots:
        if not r.exists() or not r.is_dir():
            continue
        for p in _iter_files(r):
            if p.name != "characters.json":
                continue
            rp = p.resolve()
            if rp not in char_candidates:
                char_candidates.append(rp)

    char_touched = 0
    char_migrated = 0
    for p in char_candidates:
        res = _migrate_character_store(p, apply=args.apply)
        if res == "missing":
            continue
        if res == "failed":
            print(f"WARN: CharacterStore migration failed: {p}")
            continue
        if res == "skip":
            continue
        if res == "already_new":
            continue
        char_touched += 1
        if res == "migrated":
            char_migrated += 1
            print(f"UPDATED: CharacterStore migrated: {p}")
        elif res == "would_migrate":
            print(f"DRY-RUN: would migrate CharacterStore: {p}")

    print(
        "SUMMARY:"
        f" roots={len(roots)}"
        f" missing_roots={missing_roots}"
        f" encrypted_candidates={touched}"
        f" encrypted_updated={updated}"
        f" encrypted_skipped_unknown_format={skipped_bad_format}"
        f" charstore_candidates={char_touched}"
        f" charstore_migrated={char_migrated}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
