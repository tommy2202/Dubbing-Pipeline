from __future__ import annotations

from dubbing_pipeline.doctor.cli import doctor
from dubbing_pipeline.plugins.lipsync.cli import lipsync


def add_commands(cli_group) -> None:
    cli_group.add_command(lipsync)
    cli_group.add_command(doctor)


__all__ = ["add_commands", "lipsync", "doctor"]
