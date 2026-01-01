from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from anime_v2.utils.log import logger
from anime_v2.utils.net import egress_guard
from anime_v2.utils.vad import VADConfig, detect_speech_segments


@dataclass(frozen=True, slots=True)
class DiarizeConfig:
    diarizer: str = "auto"  # auto|pyannote|speechbrain|heuristic
    enable_pyannote: bool = bool(int(os.environ.get("ENABLE_PYANNOTE", "0") or "0"))
    hf_token: str | None = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    pyannote_model: str = "pyannote/speaker-diarization-3.1"
    vad: VADConfig = VADConfig()
    max_speakers: int = 4


def _intersect_with_vad(utts: list[dict], speech: list[tuple[float, float]]) -> list[dict]:
    if not speech:
        return utts
    out = []
    for u in utts:
        us, ue = float(u["start"]), float(u["end"])
        dur = max(1e-6, ue - us)
        ov = 0.0
        for ss, se in speech:
            ov += max(0.0, min(ue, se) - max(us, ss))
        if ov / dur >= 0.2:  # require at least some speech overlap
            out.append(u)
    return out


def _pyannote(audio_path: Path, cfg: DiarizeConfig) -> list[dict]:
    from pyannote.audio import Pipeline  # type: ignore

    with egress_guard():
        pipeline = Pipeline.from_pretrained(cfg.pyannote_model, use_auth_token=cfg.hf_token)
        diar = pipeline(str(audio_path))
    out: list[dict] = []
    labels: dict[str, int] = {}
    for turn, _, speaker in diar.itertracks(yield_label=True):
        lab = str(speaker)
        labels.setdefault(lab, len(labels) + 1)
        out.append(
            {
                "start": float(turn.start),
                "end": float(turn.end),
                "speaker": f"SPEAKER_{labels[lab]:02d}",
                "conf": 0.9,
            }
        )
    return out


def _speechbrain_cluster(audio_path: Path, device: str, cfg: DiarizeConfig) -> list[dict]:
    # VAD segments -> ECAPA embeddings -> choose k -> spectral clustering
    from anime_v2.utils.embeds import ecapa_embedding

    speech = detect_speech_segments(audio_path, cfg.vad)
    segs = []
    for i, (s, e) in enumerate(speech):
        if e - s <= 0:
            continue
        segs.append((i, s, e))
    if not segs:
        return []

    # Extract segment wavs on the fly to temp files under same dir
    tmp_dir = audio_path.parent / "_sb_segments"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    import numpy as np  # type: ignore

    embs = []
    kept = []
    for idx, s, e in segs:
        seg_wav = tmp_dir / f"{idx:04d}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{s:.3f}",
                "-to",
                f"{e:.3f}",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                str(seg_wav),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        emb = ecapa_embedding(seg_wav, device=device)
        if emb is None:
            continue
        embs.append(emb)
        kept.append((s, e))

    if not embs:
        return []

    X = np.stack(embs, axis=0)
    n = X.shape[0]
    if n == 1:
        return [{"start": kept[0][0], "end": kept[0][1], "speaker": "SPEAKER_01", "conf": 0.6}]

    try:
        from sklearn.cluster import SpectralClustering  # type: ignore
        from sklearn.metrics import silhouette_score  # type: ignore
    except Exception as ex:
        logger.warning("sklearn unavailable for clustering (%s)", ex)
        # fallback to 2-speaker alternating
        out = []
        for i, (s, e) in enumerate(kept):
            out.append({"start": s, "end": e, "speaker": f"SPEAKER_{(i % 2) + 1:02d}", "conf": 0.4})
        return out

    # Choose k by silhouette on cosine distance (approx via dot since normalized)
    max_k = min(int(cfg.max_speakers), n)
    best_k = 1
    best_score = -math.inf
    # Precompute cosine similarity matrix
    S = X @ X.T
    D = 1.0 - S
    for k in range(2, max_k + 1):
        try:
            labels = SpectralClustering(
                n_clusters=k, affinity="nearest_neighbors", assign_labels="kmeans"
            ).fit_predict(X)
            sc = float(silhouette_score(D, labels, metric="precomputed"))
            if sc > best_score:
                best_score = sc
                best_k = k
        except Exception:
            continue

    if best_k == 1:
        labels = np.zeros((n,), dtype=int)
    else:
        labels = SpectralClustering(
            n_clusters=best_k, affinity="nearest_neighbors", assign_labels="kmeans"
        ).fit_predict(X)

    out = []
    for (s, e), lab in zip(kept, labels, strict=False):
        out.append(
            {
                "start": float(s),
                "end": float(e),
                "speaker": f"SPEAKER_{int(lab)+1:02d}",
                "conf": 0.7,
            }
        )
    return out


def _heuristic(audio_path: Path, cfg: DiarizeConfig) -> list[dict]:
    speech = detect_speech_segments(audio_path, cfg.vad)
    out: list[dict] = []
    if not speech:
        # Fallback: treat whole file as one speech segment to avoid "no diarization".
        try:
            import wave

            with wave.open(str(audio_path), "rb") as wf:
                dur = wf.getnframes() / float(wf.getframerate() or 16000)
        except Exception:
            dur = 0.0
        if dur > 0:
            return [{"start": 0.0, "end": float(dur), "speaker": "SPEAKER_01", "conf": 0.2}]
        return out
    # Alternate speakers between segments; if only one segment, single speaker.
    for i, (s, e) in enumerate(speech):
        out.append(
            {
                "start": float(s),
                "end": float(e),
                "speaker": f"SPEAKER_{(i % 2) + 1:02d}",
                "conf": 0.3,
            }
        )
    return out


def diarize(audio_path: str, device: str, cfg: DiarizeConfig) -> list[dict]:
    """
    Return utterances: [{start,end,speaker,conf}]
    Never raises: on failure, falls back to heuristic.
    """
    p = Path(audio_path)
    if not p.exists():
        return []

    # Base VAD segments to filter music-only windows
    speech = detect_speech_segments(p, cfg.vad)

    choice = (cfg.diarizer or "auto").lower()
    tried = []

    def want_pyannote() -> bool:
        return cfg.enable_pyannote and bool(cfg.hf_token)

    if choice == "auto":
        choice = "pyannote" if want_pyannote() else "speechbrain"

    # try requested then fallbacks
    for engine in [choice, "speechbrain", "heuristic"]:
        if engine in tried:
            continue
        tried.append(engine)
        try:
            if engine == "pyannote":
                if not want_pyannote():
                    raise RuntimeError("pyannote disabled or missing token")
                utts = _pyannote(p, cfg)
            elif engine == "speechbrain":
                utts = _speechbrain_cluster(p, device=device, cfg=cfg)
            else:
                utts = _heuristic(p, cfg)

            utts = _intersect_with_vad(utts, speech)
            # sort and merge small gaps of same speaker
            utts.sort(key=lambda u: (float(u["start"]), float(u["end"])))
            merged: list[dict] = []
            for u in utts:
                if not merged:
                    merged.append(u)
                    continue
                prev = merged[-1]
                if (
                    u["speaker"] == prev["speaker"]
                    and float(u["start"]) - float(prev["end"]) <= 0.4
                ):
                    prev["end"] = max(float(prev["end"]), float(u["end"]))
                    prev["conf"] = max(float(prev.get("conf", 0.0)), float(u.get("conf", 0.0)))
                else:
                    merged.append(u)
            if not merged:
                raise RuntimeError("no segments")
            logger.info("diarize engine=%s segments=%s", engine, len(merged))
            return merged
        except Exception as ex:
            logger.warning("diarize engine=%s failed (%s)", engine, ex)
            continue

    return []
