from __future__ import annotations

import json
from pathlib import Path

import click

from anime_v2.config import get_settings
from anime_v2.voice_memory.store import VoiceMemoryStore


@click.group(help="Character utilities (voice memory + delivery profiles).")
def character() -> None:
    pass


def _store() -> VoiceMemoryStore:
    s = get_settings()
    root = Path(s.voice_memory_dir).resolve()
    return VoiceMemoryStore(root)


@character.command("set-rate")
@click.argument("character_id", type=str)
@click.argument("rate_mul", type=float)
def set_rate(character_id: str, rate_mul: float) -> None:
    """
    Set per-character speaking rate multiplier (applied to prosody rate).
    """
    st = _store()
    st.ensure_character(character_id=str(character_id))
    st.set_character_rate_mul(str(character_id), float(rate_mul))
    click.echo(json.dumps({"ok": True, "character_id": str(character_id), "rate_mul": float(rate_mul)}, indent=2, sort_keys=True))


@character.command("set-style")
@click.argument("character_id", type=str)
@click.argument("pause_style", type=click.Choice(["default", "tight", "normal", "dramatic"], case_sensitive=False))
def set_style(character_id: str, pause_style: str) -> None:
    """
    Set per-character pause style (affects pause tail scaling when expressive/director adds pauses).
    """
    st = _store()
    st.ensure_character(character_id=str(character_id))
    st.set_character_pause_style(str(character_id), str(pause_style).lower())
    click.echo(
        json.dumps(
            {"ok": True, "character_id": str(character_id), "pause_style": str(pause_style).lower()},
            indent=2,
            sort_keys=True,
        )
    )


@character.command("set-expressive")
@click.argument("character_id", type=str)
@click.argument("strength", type=float)
def set_expressive(character_id: str, strength: float) -> None:
    """
    Set per-character expressive strength override (0..1).
    """
    st = _store()
    st.ensure_character(character_id=str(character_id))
    st.set_character_expressive_strength(str(character_id), float(strength))
    click.echo(
        json.dumps(
            {"ok": True, "character_id": str(character_id), "expressive_strength": float(strength)},
            indent=2,
            sort_keys=True,
        )
    )


@character.command("set-voice-mode")
@click.argument("character_id", type=str)
@click.argument("voice_mode", type=click.Choice(["clone", "preset", "single"], case_sensitive=False))
def set_voice_mode(character_id: str, voice_mode: str) -> None:
    """
    Set per-character preferred voice mode (clone|preset|single).
    """
    st = _store()
    st.ensure_character(character_id=str(character_id))
    st.set_character_preferred_voice_mode(str(character_id), str(voice_mode).lower())
    click.echo(
        json.dumps(
            {"ok": True, "character_id": str(character_id), "preferred_voice_mode": str(voice_mode).lower()},
            indent=2,
            sort_keys=True,
        )
    )

