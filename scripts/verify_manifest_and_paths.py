from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from anime_v2.jobs.models import Job, JobState, Visibility, now_utc
from anime_v2.library.manifest import read_manifest, write_manifest
from anime_v2.library.paths import ensure_library_dir, get_job_output_root, mirror_outputs_best_effort
from anime_v2.library.normalize import series_to_slug


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        out_root = root / "Output"
        os.environ["ANIME_V2_OUTPUT_DIR"] = str(out_root)

        job = Job(
            id="job_123",
            owner_id="u_test",
            video_path=str(root / "Input" / "Test.mp4"),
            duration_s=1.0,
            mode="low",
            device="cpu",
            src_lang="ja",
            tgt_lang="en",
            created_at=now_utc(),
            updated_at=now_utc(),
            state=JobState.DONE,
            progress=1.0,
            message="Done",
            output_mkv="",
            output_srt="",
            work_dir="",
            log_path="",
            error=None,
            series_title="My Show",
            series_slug=series_to_slug("My Show"),
            season_number=1,
            episode_number=2,
            visibility=Visibility.private,
        )

        # Canonical output root (legacy) must be stable.
        out_dir = get_job_output_root(job)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Create fake outputs.
        master = out_dir / "dub.mkv"
        master.write_bytes(b"\x00\x01fake")
        (out_dir / "logs").mkdir(parents=True, exist_ok=True)
        (out_dir / "qa").mkdir(parents=True, exist_ok=True)
        (out_dir / "qa" / "summary.json").write_text('{"score": 0.5}', encoding="utf-8")

        lib_dir = ensure_library_dir(job)
        assert lib_dir is not None

        mirror_outputs_best_effort(
            job=job,
            library_dir=lib_dir,
            master=master,
            mobile=None,
            hls_index=None,
            output_dir=out_dir,
        )

        man_path = write_manifest(
            job=job,
            outputs={
                "library_dir": str(lib_dir),
                "master": str(master),
                "mobile": None,
                "hls_index": None,
                "logs_dir": str((out_dir / "logs").resolve()),
                "qa_dir": str((out_dir / "qa").resolve()),
            },
            extra={"test": True},
        )
        assert man_path.exists()

        man = read_manifest(man_path)
        assert man is not None
        for k in [
            "job_id",
            "created_at",
            "status",
            "mode",
            "series_title",
            "series_slug",
            "season_number",
            "episode_number",
            "owner_user_id",
            "visibility",
            "paths",
            "urls",
        ]:
            assert k in man, k

        assert man["job_id"] == "job_123"
        assert man["series_slug"] == series_to_slug("My Show")
        assert int(man["season_number"]) == 1
        assert int(man["episode_number"]) == 2
        assert man["paths"]["master"] and Path(man["paths"]["master"]).exists()
        assert man["urls"]["master"] and str(man["urls"]["master"]).startswith("/files/")

        # Ensure manifest is valid JSON text on disk.
        json.loads(man_path.read_text(encoding="utf-8"))

    print("verify_manifest_and_paths: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

