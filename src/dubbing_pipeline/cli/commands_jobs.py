from __future__ import annotations

from dubbing_pipeline.overrides.cli import overrides
from dubbing_pipeline.qa.cli import qa
from dubbing_pipeline.review.cli import review


def add_commands(cli_group) -> None:
    cli_group.add_command(review)
    cli_group.add_command(qa)
    cli_group.add_command(overrides)


__all__ = ["add_commands", "review", "qa", "overrides"]
