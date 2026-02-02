from __future__ import annotations

from dubbing_pipeline.character.cli import character
from dubbing_pipeline.voice_memory.cli import voice


def add_commands(cli_group) -> None:
    cli_group.add_command(voice)
    cli_group.add_command(character)


__all__ = ["add_commands", "voice", "character"]
