from __future__ import annotations

from pathlib import Path

import pytest

from anime_v2.jobs.checkpoint import read_ckpt, stage_is_done, write_ckpt


def test_checkpoint_roundtrip_and_validation(tmp_path: Path) -> None:
    work = tmp_path / "job"
    work.mkdir()
    f = work / "x.txt"
    f.write_text("hello", encoding="utf-8")
    ckpt_path = work / ".checkpoint.json"

    write_ckpt("j1", "audio", {"audio_wav": f}, {"work_dir": str(work)}, ckpt_path=ckpt_path)
    ckpt = read_ckpt("j1", ckpt_path=ckpt_path)
    assert ckpt is not None
    assert stage_is_done(ckpt, "audio")

    # Modify file => checksum mismatch => stage not valid
    f.write_text("tampered", encoding="utf-8")
    ckpt2 = read_ckpt("j1", ckpt_path=ckpt_path)
    assert ckpt2 is not None
    assert not stage_is_done(ckpt2, "audio")

