from __future__ import annotations

import math
import subprocess
import wave
from pathlib import Path

from anime_v2.utils.config import get_settings
from anime_v2.utils.io import read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.utils.paths import segments_dir, voices_embeddings_dir, voices_registry_path


def _wav_duration_s(wav_path: Path) -> float:
    with wave.open(str(wav_path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
    return float(frames) / float(rate) if rate else 0.0


def _merge_segments(segments: list[dict], *, tol_s: float = 0.4, min_s: float = 1.0) -> list[dict]:
    """
    - Merge adjacent same-label segments when gap <= tol_s
    - Merge sub-min_s segments into nearest neighbor when possible
    """
    if not segments:
        return []

    segs = sorted(segments, key=lambda s: (float(s["start"]), float(s["end"])))

    # 1) Merge adjacent same-label segments with small gap
    merged: list[dict] = []
    cur = dict(segs[0])
    for s in segs[1:]:
        if s["diar_label"] == cur["diar_label"] and float(s["start"]) - float(cur["end"]) <= tol_s:
            cur["end"] = max(float(cur["end"]), float(s["end"]))
        else:
            merged.append(cur)
            cur = dict(s)
    merged.append(cur)

    # 2) Merge very short segments into a neighbor
    out: list[dict] = []
    i = 0
    while i < len(merged):
        s = dict(merged[i])
        dur = float(s["end"]) - float(s["start"])
        if dur >= min_s or len(merged) == 1:
            out.append(s)
            i += 1
            continue

        prev_seg = out[-1] if out else None
        next_seg = merged[i + 1] if i + 1 < len(merged) else None

        # Prefer merging into same-label neighbor if close enough
        merged_into_prev = False
        if prev_seg is not None and prev_seg["diar_label"] == s["diar_label"] and float(s["start"]) - float(prev_seg["end"]) <= tol_s:
            prev_seg["end"] = max(float(prev_seg["end"]), float(s["end"]))
            merged_into_prev = True
        elif next_seg is not None and next_seg["diar_label"] == s["diar_label"] and float(next_seg["start"]) - float(s["end"]) <= tol_s:
            next_seg = dict(next_seg)
            next_seg["start"] = min(float(next_seg["start"]), float(s["start"]))
            merged[i + 1] = next_seg
            merged_into_prev = True

        # Otherwise merge into whichever neighbor exists with smallest gap
        if not merged_into_prev:
            if prev_seg is None and next_seg is None:
                out.append(s)
            elif prev_seg is None and next_seg is not None:
                next_seg = dict(next_seg)
                next_seg["start"] = min(float(next_seg["start"]), float(s["start"]))
                merged[i + 1] = next_seg
            elif prev_seg is not None and next_seg is None:
                prev_seg["end"] = max(float(prev_seg["end"]), float(s["end"]))
            else:
                gap_prev = float(s["start"]) - float(prev_seg["end"])
                gap_next = float(next_seg["start"]) - float(s["end"])
                if gap_prev <= gap_next:
                    prev_seg["end"] = max(float(prev_seg["end"]), float(s["end"]))
                else:
                    next_seg = dict(next_seg)
                    next_seg["start"] = min(float(next_seg["start"]), float(s["start"]))
                    merged[i + 1] = next_seg

        i += 1

    # ensure numeric
    for s in out:
        s["start"] = float(s["start"])
        s["end"] = float(s["end"])
    return out


def _ffmpeg_extract_wav(src_wav: Path, dst_wav: Path, start: float, end: float) -> None:
    dst_wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-to",
            f"{end:.3f}",
            "-i",
            str(src_wav),
            "-ac",
            "1",
            "-ar",
            "16000",
            str(dst_wav),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _cosine_sim(a, b) -> float:
    # a, b: 1D numpy arrays
    import numpy as np  # type: ignore

    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return -1.0
    return float(np.dot(a, b) / denom)


def _load_registry() -> dict:
    reg_path = voices_registry_path()
    reg = read_json(reg_path, default=None)
    if not isinstance(reg, dict):
        return {"version": 1, "speakers": {}, "label_map": {}}
    reg.setdefault("version", 1)
    reg.setdefault("speakers", {})
    reg.setdefault("label_map", {})
    if not isinstance(reg["speakers"], dict):
        reg["speakers"] = {}
    if not isinstance(reg["label_map"], dict):
        reg["label_map"] = {}
    return reg


def _save_registry(reg: dict) -> None:
    write_json(voices_registry_path(), reg)


def _next_speaker_id(existing: set[str]) -> str:
    max_n = 0
    for sid in existing:
        if sid.startswith("Speaker"):
            try:
                max_n = max(max_n, int(sid.replace("Speaker", "")))
            except Exception:
                pass
    return f"Speaker{max_n + 1}"


def _assign_stable_ids(label_to_embedding: dict[str, "object"], *, threshold: float = 0.75) -> tuple[dict[str, str], dict[str, str]]:
    """
    Returns:
      - diar_label -> stable speaker_id
      - speaker_id -> embedding_path (string)
    """
    import numpy as np  # type: ignore

    reg = _load_registry()
    speakers: dict = reg["speakers"]

    # Load existing embeddings
    existing_embs: dict[str, tuple[np.ndarray, str, int]] = {}
    for sid, meta in speakers.items():
        try:
            emb_path = Path(meta["embedding_path"])
            emb = np.load(str(emb_path))
            count = int(meta.get("count", 1))
            existing_embs[sid] = (emb, str(emb_path), count)
        except Exception:
            continue

    diar_to_sid: dict[str, str] = {}
    sid_to_emb_path: dict[str, str] = {}

    for diar_label, emb_obj in label_to_embedding.items():
        emb = np.asarray(emb_obj, dtype=np.float32)

        best_sid = None
        best_sim = -math.inf
        for sid, (old_emb, _, _) in existing_embs.items():
            sim = _cosine_sim(emb, old_emb)
            if sim > best_sim:
                best_sim = sim
                best_sid = sid

        if best_sid is not None and best_sim >= threshold:
            diar_to_sid[diar_label] = best_sid
            sid_to_emb_path[best_sid] = existing_embs[best_sid][1]

            # Update embedding (running average)
            old_emb, old_path, old_count = existing_embs[best_sid]
            new_count = old_count + 1
            updated = (old_emb * old_count + emb) / float(new_count)
            np.save(old_path, updated.astype(np.float32))
            speakers[best_sid] = {"embedding_path": old_path, "count": new_count}
            existing_embs[best_sid] = (updated, old_path, new_count)
        else:
            sid = _next_speaker_id(set(speakers.keys()))
            emb_dir = voices_embeddings_dir()
            emb_dir.mkdir(parents=True, exist_ok=True)
            emb_path = emb_dir / f"{sid}.npy"
            np.save(str(emb_path), emb.astype(np.float32))
            speakers[sid] = {"embedding_path": str(emb_path), "count": 1}
            existing_embs[sid] = (emb, str(emb_path), 1)
            diar_to_sid[diar_label] = sid
            sid_to_emb_path[sid] = str(emb_path)

    reg["speakers"] = speakers
    # Do not update label_map here; diar_label values are not stable across files.
    _save_registry(reg)
    return diar_to_sid, sid_to_emb_path


def run(
    audio_path: Path,
    out_dir: Path,
    *,
    diarization_model: str | None = None,
    similarity_threshold: float = 0.75,
) -> tuple[list[dict], dict[str, str]]:
    """
    Returns:
      - segments: [{start,end,diar_label,wav_path,speaker_id}]
      - speaker_embeddings: {speaker_id: embedding_path}
    Also persists:
      - voices/registry.json + voices/embeddings/*.npy
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    seg_dir = segments_dir(out_dir)
    seg_dir.mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    model_id = diarization_model or settings.diarization_model

    raw_segments: list[dict] = []
    diar_speakers = 0

    # Try pyannote; fall back to single-speaker segment if unavailable.
    try:
        from pyannote.audio import Pipeline  # type: ignore

        logger.info("[v2] Diarization: loading %s", model_id)
        pipeline = Pipeline.from_pretrained(model_id, use_auth_token=settings.hf_token)
        diar = pipeline(str(audio_path))

        labels = set()
        for turn, _, speaker in diar.itertracks(yield_label=True):
            raw_segments.append(
                {
                    "start": float(turn.start),
                    "end": float(turn.end),
                    "diar_label": str(speaker),
                }
            )
            labels.add(str(speaker))
        diar_speakers = len(labels)
    except Exception as ex:
        dur = _wav_duration_s(audio_path)
        logger.warning("[v2] Diarization unavailable (%s). Falling back to single speaker.", ex)
        raw_segments = [{"start": 0.0, "end": float(dur), "diar_label": "SPEAKER_00"}]
        diar_speakers = 1

    merged = _merge_segments(raw_segments, tol_s=0.4, min_s=1.0)

    # Extract per-segment wav files
    segments: list[dict] = []
    for idx, s in enumerate(merged):
        start = float(s["start"])
        end = float(s["end"])
        diar_label = str(s["diar_label"])
        seg_wav = seg_dir / f"{idx:04d}_{diar_label}.wav"
        try:
            _ffmpeg_extract_wav(audio_path, seg_wav, start, end)
            wav_path = str(seg_wav)
        except Exception:
            wav_path = str(audio_path)
        segments.append({"start": start, "end": end, "diar_label": diar_label, "wav_path": wav_path})

    # Build per-diar-label embedding (average across representative chunks)
    label_to_embedding: dict[str, object] = {}
    try:
        import numpy as np  # type: ignore
        from resemblyzer import VoiceEncoder, preprocess_wav  # type: ignore

        encoder = VoiceEncoder()
        by_label: dict[str, list[Path]] = {}
        for s in segments:
            by_label.setdefault(s["diar_label"], []).append(Path(s["wav_path"]))

        for diar_label, wavs in by_label.items():
            embs = []
            for wav_p in wavs:
                try:
                    wav = preprocess_wav(str(wav_p))
                    emb = encoder.embed_utterance(wav)
                    embs.append(emb)
                except Exception:
                    continue
            if embs:
                label_to_embedding[diar_label] = np.mean(np.stack(embs, axis=0), axis=0)
    except Exception as ex:
        logger.warning("[v2] Embeddings unavailable (%s). Stable IDs will still persist.", ex)

    # Stable ID assignment (if no embeddings, just map by order deterministically)
    diar_to_sid: dict[str, str]
    sid_to_emb_path: dict[str, str]
    if label_to_embedding:
        diar_to_sid, sid_to_emb_path = _assign_stable_ids(label_to_embedding, threshold=similarity_threshold)
    else:
        # Fallback: persist a best-effort mapping by diar_label.
        # (This will NOT match across episodes reliably; embeddings are required for that.)
        reg = _load_registry()
        label_map: dict[str, str] = reg.get("label_map", {})
        speakers: dict = reg.get("speakers", {})
        existing_sids = set(speakers.keys()) | set(label_map.values())
        diar_to_sid = {}
        for diar_label in sorted({s["diar_label"] for s in segments}):
            if diar_label in label_map:
                diar_to_sid[diar_label] = label_map[diar_label]
            else:
                sid = _next_speaker_id(existing_sids)
                existing_sids.add(sid)
                label_map[diar_label] = sid
                diar_to_sid[diar_label] = sid
                # Create a placeholder embedding file if numpy is available, to
                # keep the on-disk structure stable even without resemblyzer.
                try:
                    import numpy as np  # type: ignore

                    emb_dir = voices_embeddings_dir()
                    emb_dir.mkdir(parents=True, exist_ok=True)
                    emb_path = emb_dir / f"{sid}.npy"
                    if not emb_path.exists():
                        np.save(str(emb_path), np.zeros((256,), dtype=np.float32))
                    speakers.setdefault(sid, {"embedding_path": str(emb_path), "count": 0})
                except Exception:
                    pass
        reg["label_map"] = label_map
        reg["speakers"] = speakers
        _save_registry(reg)
        sid_to_emb_path = {sid: meta["embedding_path"] for sid, meta in speakers.items() if isinstance(meta, dict) and "embedding_path" in meta}

    for s in segments:
        s["speaker_id"] = diar_to_sid.get(s["diar_label"], "Speaker1")

    stable_speakers = len(set(s["speaker_id"] for s in segments))
    logger.info("[v2] Diarization: diar_speakers=%s stable_speakers=%s segments=%s", diar_speakers, stable_speakers, len(segments))
    return segments, sid_to_emb_path

