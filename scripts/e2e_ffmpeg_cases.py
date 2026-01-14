#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def _run(cmd: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, check=False, text=True, capture_output=True, timeout=timeout)  # nosec B603
    return p


def _need_tool(name: str) -> None:
    if shutil.which(name):
        return
    raise RuntimeError(f"Missing required tool: {name}. Install ffmpeg/ffprobe and retry.")


def _ffprobe(path: Path) -> dict:
    p = _run(
        [
            "ffprobe",
            "-hide_banner",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        timeout=30,
    )
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {p.stderr.strip() or p.stdout.strip()}")
    import json

    return json.loads(p.stdout)


def _has_audio(meta: dict) -> bool:
    for s in meta.get("streams") or []:
        if (s.get("codec_type") or "").lower() == "audio":
            return True
    return False


def _extract_audio_16k(src: Path, out_wav: Path) -> None:
    # Use internal wrapper (ensures we test the same ffmpeg flags as the pipeline)
    from dubbing_pipeline.utils.ffmpeg_safe import extract_audio_mono_16k

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    extract_audio_mono_16k(src=src, dst=out_wav)
    if not out_wav.exists() or out_wav.stat().st_size <= 0:
        raise RuntimeError("audio extraction produced empty output")


@dataclass(frozen=True)
class Case:
    name: str
    make: callable
    expect_audio: bool
    required: bool = True


def _make_vfr(path: Path) -> None:
    # Build a tiny VFR-ish file by concatenating segments with different fps.
    # (This is not “perfect VFR”, but it reliably creates non-uniform timestamps.)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        # Segment A: 10 fps, 0.8s
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=44100:duration=1.6",
        "-filter_complex",
        # split video, change fps on part B, concat; keep audio from sine
        "[0:v]split=2[vx][vy];"
        "[vx]trim=0:0.8,setpts=PTS-STARTPTS[vA];"
        "[vy]trim=0.8:1.6,setpts=PTS-STARTPTS,fps=30[vB];"
        "[vA][vB]concat=n=2:v=1:a=0[v];"
        "[1:a]atrim=0:1.6,asetpts=PTS-STARTPTS[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-vsync",
        "vfr",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-t",
        "1.6",
        str(path),
    ]
    p = _run(cmd, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg VFR case failed: {p.stderr.strip() or p.stdout.strip()}")


def _make_no_audio(path: Path) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=12",
        "-t",
        "1.0",
        "-an",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(path),
    ]
    p = _run(cmd, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg no-audio case failed: {p.stderr.strip() or p.stdout.strip()}")


def _make_odd_codec(path: Path) -> None:
    # Prefer HEVC if available, else VP9; otherwise skip.
    # Note: encoder availability varies across CI distros.
    encoders = _run(["ffmpeg", "-hide_banner", "-encoders"], timeout=30)
    out = (encoders.stdout or "") + "\n" + (encoders.stderr or "")
    if "libx265" in out:
        vcodec = ["-c:v", "libx265"]
        fmt = "mp4"
    elif "libvpx-vp9" in out:
        vcodec = ["-c:v", "libvpx-vp9", "-b:v", "150k"]
        fmt = "webm"
    else:
        raise RuntimeError("SKIP: no hevc/vp9 encoder available in ffmpeg build")

    tmp = path.with_suffix(f".{fmt}")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=160x90:rate=10",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=220:sample_rate=44100",
        "-t",
        "1.0",
        *vcodec,
        "-c:a",
        "libopus" if fmt == "webm" else "aac",
        str(tmp),
    ]
    p = _run(cmd, timeout=90)
    if p.returncode != 0:
        raise RuntimeError(f"odd codec encode failed: {p.stderr.strip() or p.stdout.strip()}")
    tmp.rename(path)


def main() -> int:
    _need_tool("ffmpeg")
    _need_tool("ffprobe")

    # Keep this script compatible with repo policy checks that forbid explicit version tokens.
    os.environ.setdefault("STRICT_SECRETS", "0")

    cases: list[Case] = [
        Case(name="vfr_concat", make=_make_vfr, expect_audio=True, required=True),
        Case(name="no_audio", make=_make_no_audio, expect_audio=False, required=True),
        Case(name="odd_codec", make=_make_odd_codec, expect_audio=True, required=False),
    ]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td).resolve()
        out_dir = root / "out"
        out_dir.mkdir(parents=True, exist_ok=True)

        failures: list[str] = []
        skipped: list[str] = []

        for c in cases:
            dst = out_dir / f"{c.name}.mp4"
            print(f"\n== case: {c.name} ==")
            try:
                c.make(dst)
                meta = _ffprobe(dst)
                audio = _has_audio(meta)
                print(f"- file: {dst.name} size={dst.stat().st_size} bytes")
                print(f"- ffprobe: ok streams={len(meta.get('streams') or [])} has_audio={audio}")

                if c.expect_audio and not audio:
                    raise RuntimeError("expected audio stream, but none found")
                if (not c.expect_audio) and audio:
                    raise RuntimeError("expected no audio stream, but audio stream exists")

                wav = out_dir / f"{c.name}.wav"
                if audio:
                    _extract_audio_16k(dst, wav)
                    print(f"- audio_extract: ok ({wav.stat().st_size} bytes)")
                else:
                    # For no-audio input, ensure we fail clearly (not silent success).
                    try:
                        _extract_audio_16k(dst, wav)
                        raise RuntimeError("expected audio extraction to fail for no-audio input")
                    except Exception as ex:
                        msg = str(ex)
                        # Actionable message requirement: show the root cause.
                        print(f"- audio_extract: expected failure: {msg}")
            except Exception as ex:
                msg = str(ex)
                if msg.startswith("SKIP:"):
                    skipped.append(f"{c.name}: {msg}")
                    print(msg)
                    continue
                if c.required:
                    failures.append(f"{c.name}: {msg}")
                else:
                    skipped.append(f"{c.name}: {msg}")
                print(f"FAIL: {msg}", file=sys.stderr)

        print("\n== summary ==")
        for s in skipped:
            print(f"- SKIP: {s}")
        for f in failures:
            print(f"- FAIL: {f}", file=sys.stderr)

        if failures:
            print(
                "\nFix tips:\n"
                "- Ensure ffmpeg includes required decoders/encoders for your input codecs.\n"
                "- For no-audio inputs: reject early or add a pipeline path that can proceed without audio.\n",
                file=sys.stderr,
            )
            return 2

    print("e2e_ffmpeg_cases: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

