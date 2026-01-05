from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_copy, atomic_write_text, read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.voice_memory.store import VoiceMemoryStore, _now  # noqa: SLF001


def _safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _ts_id() -> str:
    # sortable-ish and filesystem-safe
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime())


def _copy_tree(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy(src, dst)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def _load_json(path: Path) -> Any:
    return read_json(path, default=None)


@dataclass(frozen=True, slots=True)
class MergeBackup:
    merge_id: str
    backup_dir: Path
    store_root: Path
    from_id: str
    to_id: str


def create_merge_backup(*, store_root: Path, from_id: str, to_id: str) -> MergeBackup:
    """
    Backup the voice memory state needed to undo a merge.
    Stores under: <store_root>/backups/<timestamp>_<from>_to_<to>/
    """
    root = Path(store_root).resolve()
    ts = _ts_id()
    merge_id = f"{ts}_{from_id}_to_{to_id}"
    bdir = root / "backups" / merge_id
    _safe_mkdir(bdir)

    # backup characters + full episodes folder (safe + small)
    _copy_tree(root / "characters.json", bdir / "characters.json")
    _copy_tree(root / "episodes", bdir / "episodes")

    # backup embeddings for affected characters only
    _copy_tree(root / "embeddings" / from_id, bdir / "embeddings" / from_id)
    _copy_tree(root / "embeddings" / to_id, bdir / "embeddings" / to_id)

    manifest = {
        "version": 1,
        "merge_id": merge_id,
        "created_at": _now(),
        "store_root": str(root),
        "from_id": str(from_id),
        "to_id": str(to_id),
        "paths": {
            "characters": "characters.json",
            "episodes": "episodes/",
            "embeddings_from": f"embeddings/{from_id}/",
            "embeddings_to": f"embeddings/{to_id}/",
        },
    }
    write_json(bdir / "manifest.json", manifest, indent=2)
    logger.info("voice_merge_backup_created", merge_id=merge_id, backup_dir=str(bdir))
    return MergeBackup(
        merge_id=merge_id, backup_dir=bdir, store_root=root, from_id=from_id, to_id=to_id
    )


def _next_ref_name(dest_dir: Path) -> Path:
    existing = sorted([p for p in dest_dir.glob("ref_*.wav") if p.is_file()])
    n = len(existing) + 1
    return dest_dir / f"ref_{n:03d}.wav"


def _merge_embeddings(store: VoiceMemoryStore, *, from_id: str, to_id: str) -> None:
    """
    Best-effort embedding merge:
      - if both embeddings exist and have same length -> average
      - else prefer to_id, fallback to from_id
    """
    a = store.load_embedding(from_id)
    b = store.load_embedding(to_id)
    if b is None and a is None:
        return
    if b is None and a is not None:
        store.save_embedding(to_id, a, provider="merge_copy_from")
        return
    if b is not None and a is None:
        return
    assert a is not None and b is not None
    if len(a) == len(b) and len(a) > 0:
        merged = [(float(x) + float(y)) / 2.0 for x, y in zip(a, b, strict=False)]
        store.save_embedding(to_id, merged, provider="merge_avg")
    else:
        # keep to_id embedding as source of truth
        return


def _update_episode_mappings(root: Path, *, from_id: str, to_id: str) -> int:
    """
    Update episodes/<episode_key>.json mapping entries to point from from_id -> to_id.
    Returns number of mapping entries updated.
    """
    eps_dir = Path(root) / "episodes"
    if not eps_dir.exists():
        return 0
    changed = 0
    for p in sorted(eps_dir.glob("*.json")):
        data = _load_json(p)
        if not isinstance(data, dict):
            continue
        mapping = data.get("mapping")
        if not isinstance(mapping, dict):
            continue
        dirty = False
        for _diar_label, rec in mapping.items():
            if not isinstance(rec, dict):
                continue
            cid = str(rec.get("character_id") or "")
            if cid == str(from_id):
                rec["character_id"] = str(to_id)
                rec["merged_from"] = str(from_id)
                dirty = True
                changed += 1
        if dirty:
            write_json(p, data, indent=2)
    return changed


def merge_characters(
    *,
    store_root: Path,
    from_id: str,
    to_id: str,
    move_refs: bool,
    keep_alias: bool,
) -> MergeBackup:
    """
    Merge `from_id` into `to_id`:
      - backups first (reversible)
      - merges refs and embeddings
      - updates episode mappings to point at to_id
      - optionally keeps from_id as an alias tombstone
    """
    root = Path(store_root).resolve()
    from_id = str(from_id).strip()
    to_id = str(to_id).strip()
    if not from_id or not to_id:
        raise ValueError("from_id and to_id are required")
    if from_id == to_id:
        raise ValueError("from_id == to_id")

    store = VoiceMemoryStore(root)
    # ensure both records exist (so alias updates are consistent)
    store.ensure_character(character_id=from_id)
    store.ensure_character(character_id=to_id)

    backup = create_merge_backup(store_root=root, from_id=from_id, to_id=to_id)

    # merge refs
    from_dir = store.character_dir(from_id)
    to_dir = store.character_dir(to_id)
    from_refs = sorted([p for p in from_dir.glob("ref_*.wav") if p.is_file()])
    moved = 0
    for r in from_refs:
        dest = _next_ref_name(to_dir)
        atomic_copy(r, dest)
        moved += 1
        if move_refs:
            with __import__("contextlib").suppress(Exception):
                r.unlink(missing_ok=True)

    # merge embeddings
    _merge_embeddings(store, from_id=from_id, to_id=to_id)

    # update episode mappings
    ep_changed = _update_episode_mappings(root, from_id=from_id, to_id=to_id)

    # update character meta
    data = store._load_characters()  # noqa: SLF001
    chars = data.get("characters", {})
    rec_from = chars.get(from_id) if isinstance(chars, dict) else None
    rec_to = chars.get(to_id) if isinstance(chars, dict) else None
    if isinstance(rec_to, dict):
        aliases = rec_to.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        if from_id not in aliases:
            aliases.append(from_id)
        rec_to["aliases"] = sorted(set(str(x) for x in aliases if str(x).strip()))
        rec_to["updated_at"] = _now()
        rec_to["notes"] = (str(rec_to.get("notes") or "") + f"\nmerged_from={from_id}").strip()
        chars[to_id] = rec_to

    if isinstance(chars, dict):
        if keep_alias:
            if not isinstance(rec_from, dict):
                rec_from = {"character_id": from_id, "created_at": _now()}
            rec_from["alias_to"] = to_id
            rec_from["updated_at"] = _now()
            rec_from["notes"] = (str(rec_from.get("notes") or "") + f"\nalias_to={to_id}").strip()
            chars[from_id] = rec_from
        else:
            chars.pop(from_id, None)
    store._save_characters(data)  # noqa: SLF001

    # optional cleanup of empty from_dir
    if move_refs:
        with __import__("contextlib").suppress(Exception):
            if from_dir.exists() and not any(from_dir.iterdir()):
                from_dir.rmdir()

    # write merge op summary into backup
    op = {
        "version": 1,
        "merge_id": backup.merge_id,
        "applied_at": _now(),
        "from_id": from_id,
        "to_id": to_id,
        "move_refs": bool(move_refs),
        "keep_alias": bool(keep_alias),
        "refs_copied": int(moved),
        "episode_entries_updated": int(ep_changed),
    }
    atomic_write_text(
        backup.backup_dir / "merge_applied.json",
        json.dumps(op, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    logger.info(
        "voice_merge_done", merge_id=backup.merge_id, refs=moved, episode_updates=ep_changed
    )
    return backup


def undo_merge(*, store_root: Path, merge_id: str) -> None:
    """
    Restore from a merge backup.
    """
    root = Path(store_root).resolve()
    mid = str(merge_id).strip()
    if not mid:
        raise ValueError("merge_id required")
    bdir = root / "backups" / mid
    man = _load_json(bdir / "manifest.json")
    if not isinstance(man, dict):
        raise FileNotFoundError(f"Backup manifest not found: {bdir / 'manifest.json'}")

    # restore characters + episodes (replace)
    src_chars = bdir / "characters.json"
    if src_chars.exists():
        atomic_copy(src_chars, root / "characters.json")
    src_eps = bdir / "episodes"
    if src_eps.exists():
        dst_eps = root / "episodes"
        if dst_eps.exists():
            shutil.rmtree(dst_eps, ignore_errors=True)
        shutil.copytree(src_eps, dst_eps)

    # restore embeddings subdirs (replace only those present in backup)
    emb_src = bdir / "embeddings"
    if emb_src.exists():
        for sub in emb_src.iterdir():
            if not sub.is_dir():
                continue
            dst = root / "embeddings" / sub.name
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(sub, dst)

    logger.info("voice_merge_undone", merge_id=mid, backup_dir=str(bdir))
