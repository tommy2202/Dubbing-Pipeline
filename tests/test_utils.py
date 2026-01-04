from __future__ import annotations

from pathlib import Path

from anime_v2.utils.paths import (
    output_dir_for,
    output_root,
    voices_embeddings_dir,
    voices_registry_path,
    voices_root,
)
from anime_v2.utils.time import format_srt_timestamp


def test_format_srt_timestamp_basic():
    assert format_srt_timestamp(0.0) == "00:00:00,000"
    assert format_srt_timestamp(1.234) == "00:00:01,234"
    assert (
        format_srt_timestamp(61.005) == "00:01:01,004"
        or format_srt_timestamp(61.005) == "00:01:01,005"
    )


def test_path_helpers(tmp_path: Path):
    work = tmp_path
    out_root = output_root(work)
    assert out_root == work / "Output"

    v = work / "Input" / "Test.mp4"
    out_dir = output_dir_for(v, work)
    assert out_dir == work / "Output" / "Test"

    assert voices_root(work) == work / "voices"
    assert voices_registry_path(work) == work / "voices" / "registry.json"
    assert voices_embeddings_dir(work) == work / "voices" / "embeddings"
