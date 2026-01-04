from __future__ import annotations

import json
from pathlib import Path

import click

from anime_v2.config import get_settings
from anime_v2.utils.paths import output_dir_for
from anime_v2.voice_memory.audition import audition as run_audition
from anime_v2.voice_memory.store import VoiceMemoryStore
from anime_v2.voice_memory.tools import merge_characters, undo_merge


@click.group()
def voice() -> None:
    """Character Voice Memory tools (list/merge/undo/audition)."""


@voice.command("list")
def voice_list() -> None:
    s = get_settings()
    root = Path(s.voice_memory_dir).resolve()
    store = VoiceMemoryStore(root)
    items = store.list_characters()
    click.echo(json.dumps({"root": str(root), "characters": items}, indent=2, sort_keys=True))


@voice.command("merge")
@click.argument("from_id", type=str)
@click.argument("to_id", type=str)
@click.option("--move-refs", is_flag=True, default=False, help="Move ref WAVs from from_id into to_id (default: copy).")
@click.option("--keep-alias", is_flag=True, default=False, help="Keep from_id as an alias tombstone pointing to to_id.")
def voice_merge(from_id: str, to_id: str, move_refs: bool, keep_alias: bool) -> None:
    s = get_settings()
    root = Path(s.voice_memory_dir).resolve()
    backup = merge_characters(
        store_root=root,
        from_id=str(from_id),
        to_id=str(to_id),
        move_refs=bool(move_refs),
        keep_alias=bool(keep_alias),
    )
    click.echo(
        json.dumps(
            {
                "ok": True,
                "merge_id": backup.merge_id,
                "backup_dir": str(backup.backup_dir),
                "from_id": backup.from_id,
                "to_id": backup.to_id,
            },
            indent=2,
            sort_keys=True,
        )
    )


@voice.command("undo-merge")
@click.argument("merge_id", type=str)
def voice_undo_merge(merge_id: str) -> None:
    s = get_settings()
    root = Path(s.voice_memory_dir).resolve()
    undo_merge(store_root=root, merge_id=str(merge_id))
    click.echo(json.dumps({"ok": True, "merge_id": str(merge_id)}, indent=2, sort_keys=True))


@voice.command("audition")
@click.option("--text", required=True, type=str)
@click.option("--top", "top_n", default=3, type=int, show_default=True)
@click.option("--character", "character_id", default=None, type=str, help="Optional character_id from voice memory.")
@click.option("--lang", "language", default="en", show_default=True)
def voice_audition(text: str, top_n: int, character_id: str | None, language: str) -> None:
    s = get_settings()
    out_root = Path(s.output_dir).resolve()
    job_dir = out_root / f"audition_{__import__('time').strftime('%Y%m%d-%H%M%S', __import__('time').gmtime())}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "analysis").mkdir(parents=True, exist_ok=True)

    manifest = run_audition(
        text=str(text),
        top_n=int(top_n),
        character_id=(str(character_id).strip() if character_id else None),
        out_job_dir=job_dir,
        language=str(language),
    )
    click.echo(json.dumps({"ok": True, "job_dir": str(job_dir), "audition_dir": str(job_dir / "audition"), "manifest": str(job_dir / "audition" / "manifest.json"), "results": manifest.get("results", [])}, indent=2, sort_keys=True))

