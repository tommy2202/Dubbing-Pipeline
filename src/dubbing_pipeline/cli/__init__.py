from __future__ import annotations

from .args_common import DefaultGroup
from .commands_admin import doctor, lipsync
from .commands_jobs import overrides, qa, review
from .commands_library import character, voice
from .commands_run import run
from . import commands_admin, commands_jobs, commands_library

cli = DefaultGroup(name="dubbing-pipeline", help="dubbing-pipeline CLI (run + review)")  # type: ignore[assignment]
cli.add_command(run)
commands_jobs.add_commands(cli)
commands_library.add_commands(cli)
commands_admin.add_commands(cli)

__all__ = [
    "DefaultGroup",
    "cli",
    "run",
    "review",
    "qa",
    "overrides",
    "voice",
    "character",
    "lipsync",
    "doctor",
]
