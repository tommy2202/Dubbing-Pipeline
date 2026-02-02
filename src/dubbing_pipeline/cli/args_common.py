from __future__ import annotations

from pathlib import Path

import click


class DefaultGroup(click.Group):
    """
    Click group that supports a default command.

    This preserves backwards-compatible usage:
      dubbing-pipeline Input/Test.mp4 ...
    while enabling subcommands:
      dubbing-pipeline review ...
    """

    def __init__(self, *args, default_cmd: str = "run", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.default_cmd = str(default_cmd)

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args:
            first = args[0]
            if first not in self.commands:
                args.insert(0, self.default_cmd)
        return super().parse_args(ctx, args)


def _select_device(device: str) -> str:
    device = device.lower()
    if device in {"cpu", "cuda"}:
        return device
    if device != "auto":
        return "cpu"

    try:
        import torch  # type: ignore

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def _parse_srt_to_cues(srt_path: Path) -> list[dict]:
    """
    Parse SRT into cues: [{start,end,text}]
    """
    from dubbing_pipeline.utils.cues import parse_srt_to_cues

    return parse_srt_to_cues(srt_path)


def _assign_speakers(cues: list[dict], diar_segments: list[dict] | None) -> list[dict]:
    from dubbing_pipeline.utils.cues import assign_speakers

    return assign_speakers(cues, diar_segments)
