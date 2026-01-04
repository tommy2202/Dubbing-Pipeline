from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(cmd)}\n\nSTDERR:\n{p.stderr}")
    return p


def _ffprobe_streams(path: Path) -> dict:
    p = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    return json.loads(p.stdout)


def main() -> int:
    try:
        _run(["ffmpeg", "-version"])
        _run(["ffprobe", "-version"])
    except Exception as ex:
        print(f"[verify_multitrack_mux] ffmpeg/ffprobe missing: {ex}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="verify_multitrack_mux_") as td:
        d = Path(td)
        video = d / "in.mp4"
        # Tiny deterministic video (2s)
        _run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=320x240:rate=24",
                "-t",
                "2",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ]
        )

        def mk_tone(out_wav: Path, hz: int) -> None:
            _run(
                [
                    "ffmpeg",
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency={hz}:sample_rate=48000",
                    "-t",
                    "2",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(out_wav),
                ]
            )

        orig = d / "original_full.wav"
        dub = d / "dubbed_full.wav"
        bg = d / "background_only.wav"
        dlg = d / "dialogue_only.wav"
        mk_tone(orig, 440)
        mk_tone(dub, 660)
        mk_tone(bg, 220)
        mk_tone(dlg, 880)

        # Use repo exporter to ensure metadata behavior is correct
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from anime_v2.stages.export import export_mkv_multitrack  # noqa: E402

        out = d / "out.mkv"
        export_mkv_multitrack(
            video_in=video,
            tracks=[
                {"path": str(orig), "title": "Original (JP)", "language": "jpn", "default": "0"},
                {"path": str(dub), "title": "Dubbed (EN)", "language": "eng", "default": "1"},
                {"path": str(bg), "title": "Background Only", "language": "und", "default": "0"},
                {"path": str(dlg), "title": "Dialogue Only", "language": "eng", "default": "0"},
            ],
            srt=None,
            out_path=out,
        )

        data = _ffprobe_streams(out)
        streams = data.get("streams", [])
        astreams = [s for s in streams if s.get("codec_type") == "audio"]
        vstreams = [s for s in streams if s.get("codec_type") == "video"]
        if len(vstreams) != 1:
            raise AssertionError(f"expected 1 video stream, got {len(vstreams)}")
        if len(astreams) != 4:
            raise AssertionError(f"expected 4 audio streams, got {len(astreams)}")

        # Validate language/title metadata presence (best-effort; some ffprobe builds may omit)
        titles = []
        langs = []
        for s in astreams:
            tags = s.get("tags", {}) or {}
            titles.append(str(tags.get("title", "")))
            langs.append(str(tags.get("language", "")))
        if not any("Original" in t for t in titles):
            raise AssertionError(f"missing expected audio title tags: {titles}")
        if not any(l in {"jpn", "eng", "und"} for l in langs):
            raise AssertionError(f"missing expected language tags: {langs}")

        print("[verify_multitrack_mux] OK: mkv has 1 video + 4 audio tracks")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

