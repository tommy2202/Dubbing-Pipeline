from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path


def _touch(p: Path, size: int = 16) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)


def _make_fake_job(root: Path) -> Path:
    job = root / "Output" / "job_test"
    job.mkdir(parents=True, exist_ok=True)

    # final outputs
    _touch(job / "dub.mkv", 128)
    _touch(job / "dub.mp4", 128)

    # essential logs/manifests
    _touch(job / "logs" / "pipeline.log", 32)
    _touch(job / "logs" / "summary.json", 32)
    _touch(job / "manifests" / "audio.json", 32)

    # important intermediates
    _touch(job / "translated.json", 32)
    _touch(job / "out.srt", 32)
    _touch(job / "analysis" / "effective_settings.json", 32)

    # heavy intermediates
    _touch(job / "stems" / "background.wav", 4096)
    _touch(job / "stems" / "dialogue.wav", 4096)
    _touch(job / "segments" / "000.wav", 2048)
    _touch(job / "chunks" / "chunk_000.wav", 2048)
    _touch(job / "audio" / "tracks" / "original_full.wav", 4096)
    _touch(job / "tmp" / "scratch.tmp", 64)

    return job


def _paths_under(job: Path) -> set[Path]:
    return {p for p in job.rglob("*") if p.exists()}

def _normalize(paths: set[Path]) -> set[Path]:
    # retention always writes a report; ignore it for "no deletion" checks
    return {p for p in paths if p.name != "retention_report.json"}


def main() -> int:
    from anime_v2.storage.retention import apply_retention

    with tempfile.TemporaryDirectory(prefix="verify_retention_") as td:
        root = Path(td)
        job = _make_fake_job(root)

        # FULL: keep everything
        before = _normalize(_paths_under(job))
        rep = apply_retention(job, "full", dry_run=False)
        after = _normalize(_paths_under(job))
        assert before == after, "full policy must not delete anything"
        assert (job / "analysis" / "retention_report.json").exists()
        assert rep["policy"] == "full"

        # BALANCED: delete heavy dirs/files but keep finals/logs/manifests
        job2 = _make_fake_job(root / "case2")
        rep2 = apply_retention(job2, "balanced", dry_run=False)
        assert (job2 / "dub.mkv").exists()
        assert (job2 / "logs" / "pipeline.log").exists()
        assert (job2 / "manifests" / "audio.json").exists()
        assert not (job2 / "stems").exists() or not any((job2 / "stems").rglob("*"))
        assert not (job2 / "segments").exists() or not any((job2 / "segments").rglob("*"))
        assert not (job2 / "chunks").exists() or not any((job2 / "chunks").rglob("*"))
        assert not (job2 / "audio" / "tracks").exists() or not any((job2 / "audio" / "tracks").rglob("*"))
        assert (job2 / "analysis" / "retention_report.json").exists()
        assert rep2["policy"] == "balanced"

        # MINIMAL: keep finals + essential logs/manifests + analysis; delete heavies
        job3 = _make_fake_job(root / "case3")
        rep3 = apply_retention(job3, "minimal", dry_run=False)
        assert (job3 / "dub.mkv").exists()
        assert (job3 / "dub.mp4").exists()
        assert (job3 / "logs" / "pipeline.log").exists()
        assert (job3 / "manifests" / "audio.json").exists()
        assert (job3 / "analysis" / "effective_settings.json").exists()
        assert not (job3 / "stems").exists()
        assert not (job3 / "segments").exists()
        assert not (job3 / "chunks").exists()
        assert not (job3 / "audio" / "tracks").exists()
        assert not (job3 / "tmp").exists()
        assert (job3 / "analysis" / "retention_report.json").exists()
        assert rep3["policy"] == "minimal"

        # DRY RUN: should not delete
        job4 = _make_fake_job(root / "case4")
        before4 = _normalize(_paths_under(job4))
        rep4 = apply_retention(job4, "minimal", dry_run=True)
        after4 = _normalize(_paths_under(job4))
        assert before4 == after4, "dry-run must not delete anything"
        assert rep4["dry_run"] is True

        # Quick print for humans
        print("verify_retention: OK")
        print(json.dumps({"full": len(rep.get("deleted", [])), "balanced": len(rep2.get("deleted", [])), "minimal": len(rep3.get("deleted", []))}, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

