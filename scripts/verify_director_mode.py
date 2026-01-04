from __future__ import annotations

import math
import wave
from pathlib import Path

from config.settings import get_safe_config_report


def _write_tone(path: Path, *, seconds: float, sr: int = 16000, amp: float = 0.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = max(1, int(seconds * sr))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        for i in range(n):
            t = i / sr
            v = int(amp * math.sin(2.0 * math.pi * 440.0 * t) * 32767)
            wf.writeframesraw(int(v).to_bytes(2, "little", signed=True))


def main() -> int:
    print("safe_config_report:", get_safe_config_report())
    from anime_v2.expressive.director import plan_for_segment, write_director_plans_jsonl

    tmp = Path("_tmp_director")
    tmp.mkdir(parents=True, exist_ok=True)
    wav = tmp / "audio.wav"
    _write_tone(wav, seconds=2.0, amp=0.25)

    plans = [
        plan_for_segment(
            segment_id=1,
            text="What?!",
            start_s=0.0,
            end_s=0.8,
            source_audio_wav=wav,
            strength=0.8,
        ),
        plan_for_segment(
            segment_id=2,
            text="... sorry",
            start_s=0.8,
            end_s=1.6,
            source_audio_wav=wav,
            strength=0.8,
        ),
    ]
    for p in plans:
        assert 0.90 <= p.rate_mul <= 1.12
        assert 0.92 <= p.pitch_mul <= 1.12
        assert 0.85 <= p.energy_mul <= 1.20
        assert 0 <= p.pause_tail_ms <= 250

    outp = tmp / "expressive" / "director_plans.jsonl"
    write_director_plans_jsonl(plans, outp)
    assert outp.exists()
    txt = outp.read_text(encoding="utf-8")
    assert '"segment_id": 1' in txt

    print("OK: verify_director_mode passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

