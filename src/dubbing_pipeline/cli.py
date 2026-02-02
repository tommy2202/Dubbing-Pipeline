from __future__ import annotations

from .cli import (
    DefaultGroup,
    character,
    cli,
    doctor,
    lipsync,
    overrides,
    qa,
    review,
    run,
    voice,
)

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

if __name__ == "__main__":  # pragma: no cover
    cli()
