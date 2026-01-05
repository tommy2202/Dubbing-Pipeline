from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from anime_v2.utils.ffmpeg_safe import extract_audio_mono_16k, ffprobe_duration_seconds


@dataclass(frozen=True, slots=True)
class Chunk:
    idx: int
    start_s: float
    end_s: float
    wav_path: Path

    def to_dict(self) -> dict:
        d = asdict(self)
        d["wav_path"] = str(self.wav_path)
        return d


def split_audio_to_chunks(
    *,
    source_wav: Path,
    out_dir: Path,
    chunk_seconds: float = 10.0,
    overlap_seconds: float = 1.0,
    prefix: str = "chunk_",
) -> list[Chunk]:
    """
    Splits `source_wav` (any ffmpeg-readable audio) into mono 16k WAV chunks.

    Writes to:
      out_dir/<prefix><idx:03d>.wav
    """
    source_wav = Path(source_wav)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    total = float(ffprobe_duration_seconds(source_wav, timeout_s=60))
    if total <= 0:
        return []

    cs = max(2.0, float(chunk_seconds))
    ov = max(0.0, min(float(overlap_seconds), cs * 0.9))

    chunks: list[Chunk] = []
    start = 0.0
    idx = 0
    while start < total - 1e-3:
        idx += 1
        end = min(total, start + cs)
        wav_out = out_dir / f"{prefix}{idx:03d}.wav"
        extract_audio_mono_16k(
            src=source_wav,
            dst=wav_out,
            start_s=float(start),
            end_s=float(end),
            timeout_s=180,
        )
        chunks.append(Chunk(idx=idx, start_s=float(start), end_s=float(end), wav_path=wav_out))
        if end >= total:
            break
        start = max(0.0, end - ov)
    return chunks
