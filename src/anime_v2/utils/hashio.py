from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from anime_v2.utils.log import logger


def hash_wav(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_audio_from_video(path: str | Path) -> str:
    """
    Hash the *audio track* of a container file.

    We decode to raw PCM (mono, 16k) and SHA256 the byte stream so container metadata
    does not affect the key.
    """
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))

    cmd = [
        "ffmpeg",
        "-nostdin",
        "-i",
        str(p),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    h = hashlib.sha256()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as ex:
        raise RuntimeError(f"ffmpeg spawn failed: {ex}") from ex

    assert proc.stdout is not None
    try:
        for chunk in iter(lambda: proc.stdout.read(1024 * 1024), b""):
            h.update(chunk)
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        rc = proc.wait(timeout=30)
        if rc != 0:
            raise RuntimeError(f"ffmpeg failed hashing audio (exit={rc})")

    return h.hexdigest()


def speaker_signature(lang: str, speaker: str, speaker_wav_path: str | Path | None) -> str:
    """
    Signature used to scope TTS cache.
    Includes short hash of speaker wav if provided.
    """
    parts = [f"lang={lang or ''}", f"speaker={speaker or ''}"]
    if speaker_wav_path:
        p = Path(speaker_wav_path)
        if p.exists() and p.is_file():
            try:
                parts.append(f"wav={hash_wav(p)[:12]}")
            except Exception:
                parts.append("wav=err")
    return "|".join(parts)

