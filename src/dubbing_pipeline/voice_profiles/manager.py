from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.jobs.store import JobStore
from dubbing_pipeline.utils.io import atomic_copy
from dubbing_pipeline.utils.log import logger
from dubbing_pipeline.voice_memory.embeddings import compute_embedding, match_embedding


def _root(voice_store_dir: Path | None = None) -> Path:
    if voice_store_dir is not None:
        return Path(voice_store_dir).resolve()
    return Path(get_settings().voice_store_dir).resolve()


def profile_root(profile_id: str, *, voice_store_dir: Path | None = None) -> Path:
    pid = str(profile_id or "").strip()
    if not pid:
        raise ValueError("profile_id is required")
    return (_root(voice_store_dir) / "profiles" / pid).resolve()


def profile_ref_path(profile_id: str, *, voice_store_dir: Path | None = None) -> Path:
    return profile_root(profile_id, voice_store_dir=voice_store_dir) / "ref.wav"


def ensure_profile_ref(
    profile_id: str, ref_path: Path, *, voice_store_dir: Path | None = None
) -> Path:
    dest = profile_ref_path(profile_id, voice_store_dir=voice_store_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic_copy(Path(ref_path).resolve(), dest)
    return dest


def resolve_profile_ref_path(
    profile: dict[str, Any], *, voice_store_dir: Path | None = None
) -> Path | None:
    if not isinstance(profile, dict):
        return None
    meta = profile.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None
    if isinstance(meta, dict):
        raw = str(meta.get("ref_path") or "").strip()
        if raw:
            p = Path(raw).resolve()
            if p.exists():
                return p
    pid = str(profile.get("id") or "").strip()
    if pid:
        p2 = profile_ref_path(pid, voice_store_dir=voice_store_dir)
        if p2.exists():
            return p2
    return None


def _embedding_from_profile(profile: dict[str, Any]) -> list[float] | None:
    emb = profile.get("embedding_vector")
    if emb is None:
        return None
    if isinstance(emb, list):
        try:
            return [float(x) for x in emb]
        except Exception:
            return None
    if isinstance(emb, (bytes, bytearray)):
        try:
            emb = emb.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if isinstance(emb, str):
        try:
            data = json.loads(emb)
            if isinstance(data, list):
                return [float(x) for x in data]
        except Exception:
            return None
    return None


def _cosine_sim(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b, strict=False):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        na += fx * fx
        nb += fy * fy
    denom = (na ** 0.5) * (nb ** 0.5)
    if denom <= 0:
        return -1.0
    return float(dot / denom)


def _embedding_model_id(provider: str) -> str:
    s = get_settings()
    override = getattr(s, "voice_profile_embedding_model_id", None)
    if override:
        return str(override)
    return str(provider or "")


def match_profiles_for_refs(
    *,
    store: JobStore,
    series_slug: str,
    label_refs: dict[str, Path],
    allow_global: bool,
    threshold: float,
    device: str,
    voice_store_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    series_slug = str(series_slug or "").strip()
    if not series_slug or not label_refs:
        return {}
    profiles = store.list_voice_profiles(series_slug=series_slug, allow_global=bool(allow_global))
    candidates: dict[str, list[float]] = {}
    for prof in profiles:
        pid = str(prof.get("id") or "").strip()
        if not pid:
            continue
        prof_model = str(prof.get("embedding_model_id") or "").strip()
        emb = _embedding_from_profile(prof)
        if emb is None:
            ref = resolve_profile_ref_path(prof, voice_store_dir=voice_store_dir)
            if ref is not None:
                emb, provider = compute_embedding(ref, device=device)
                if emb is not None:
                    prof_model = _embedding_model_id(provider)
                    try:
                        store.upsert_voice_profile(
                            profile_id=pid,
                            display_name=str(prof.get("display_name") or ""),
                            created_by=str(prof.get("created_by") or ""),
                            scope=str(prof.get("scope") or "private"),
                            series_lock=str(prof.get("series_lock") or ""),
                            source_type=str(prof.get("source_type") or "unknown"),
                            export_allowed=bool(prof.get("export_allowed") or False),
                            share_allowed=bool(prof.get("share_allowed") or False),
                            reuse_allowed=prof.get("reuse_allowed"),
                            expires_at=prof.get("expires_at"),
                            embedding_vector=emb,
                            embedding_model_id=str(prof_model or provider or ""),
                            metadata_json=prof.get("metadata_json"),
                        )
                    except Exception:
                        pass
        if emb is None:
            continue
        candidates[pid] = emb
        if prof_model:
            prof["embedding_model_id"] = prof_model
    if not candidates:
        return {}

    out: dict[str, dict[str, Any]] = {}
    for label, ref_path in label_refs.items():
        lab = str(label or "").strip()
        if not lab:
            continue
        emb, provider = compute_embedding(Path(ref_path), device=device)
        if emb is None:
            continue
        model_id = _embedding_model_id(provider)
        candidates_filtered = {}
        if model_id:
            for pid, vec in candidates.items():
                prof = next((p for p in profiles if str(p.get("id") or "") == pid), {})
                if str(prof.get("embedding_model_id") or "").strip() == model_id:
                    candidates_filtered[pid] = vec
        else:
            candidates_filtered = candidates
        best_id, best_sim = match_embedding(
            emb, candidates_filtered, threshold=float(threshold)
        )
        if best_id is None:
            continue
        # resolve ref_path for matched profile
        prof = next((p for p in profiles if str(p.get("id") or "") == best_id), {})
        ref = resolve_profile_ref_path(prof, voice_store_dir=voice_store_dir)
        out[lab] = {
            "profile_id": str(best_id),
            "similarity": float(best_sim),
            "provider": str(provider),
            "ref_path": str(ref) if ref is not None else "",
            "matched_at": float(time.time()),
        }
    return out


def suggest_similar_profiles(
    *,
    store: JobStore,
    profile_id: str,
    embedding: list[float],
    embedding_model_id: str,
    series_slug: str,
    created_by: str,
    allow_global: bool = True,
    threshold: float | None = None,
    voice_store_dir: Path | None = None,
) -> list[dict[str, Any]]:
    series_slug = str(series_slug or "").strip()
    if not profile_id or not embedding or not series_slug:
        return []
    s = get_settings()
    thresh = float(
        threshold
        if threshold is not None
        else getattr(s, "voice_profile_suggest_threshold", 0.82)
    )
    profiles = store.list_voice_profiles()
    same_series: list[tuple[str, float, dict[str, Any]]] = []
    global_candidates: list[tuple[str, float, dict[str, Any]]] = []
    for prof in profiles:
        pid = str(prof.get("id") or "").strip()
        if not pid or pid == profile_id:
            continue
        if embedding_model_id:
            prof_model = str(prof.get("embedding_model_id") or "").strip()
            if prof_model and prof_model != embedding_model_id:
                continue
        emb = _embedding_from_profile(prof)
        if emb is None:
            ref = resolve_profile_ref_path(prof, voice_store_dir=voice_store_dir)
            if ref is not None:
                emb, provider = compute_embedding(ref, device="cpu")
                if emb is not None:
                    model_id = _embedding_model_id(provider)
                    try:
                        store.upsert_voice_profile(
                            profile_id=pid,
                            display_name=str(prof.get("display_name") or ""),
                            created_by=str(prof.get("created_by") or ""),
                            scope=str(prof.get("scope") or "private"),
                            series_lock=str(prof.get("series_lock") or ""),
                            source_type=str(prof.get("source_type") or "unknown"),
                            export_allowed=bool(prof.get("export_allowed") or False),
                            share_allowed=bool(prof.get("share_allowed") or False),
                            reuse_allowed=prof.get("reuse_allowed"),
                            expires_at=prof.get("expires_at"),
                            embedding_vector=emb,
                            embedding_model_id=str(model_id or provider or ""),
                            metadata_json=prof.get("metadata_json"),
                        )
                        prof["embedding_model_id"] = model_id
                    except Exception:
                        pass
        if emb is None:
            continue
        sim = _cosine_sim(embedding, emb)
        if sim < thresh:
            continue
        series_lock = str(prof.get("series_lock") or "").strip()
        scope = str(prof.get("scope") or "private").strip().lower()
        reuse_allowed = bool(prof.get("reuse_allowed") or False)
        if series_lock == series_slug:
            same_series.append((pid, sim, prof))
        elif allow_global and not series_lock and reuse_allowed and scope in {"global", "friends"}:
            global_candidates.append((pid, sim, prof))

    suggestions: list[dict[str, Any]] = []
    if same_series:
        pid, sim, _prof = max(same_series, key=lambda x: x[1])
        if not store.has_voice_profile_alias(profile_id, pid):
            rec = store.insert_voice_profile_suggestion(
                voice_profile_id=profile_id,
                suggested_profile_id=pid,
                similarity=float(sim),
                created_by=str(created_by or ""),
            )
            if rec:
                suggestions.append(rec)
    if global_candidates:
        pid, sim, _prof = max(global_candidates, key=lambda x: x[1])
        if not store.has_voice_profile_alias(profile_id, pid):
            rec = store.insert_voice_profile_suggestion(
                voice_profile_id=profile_id,
                suggested_profile_id=pid,
                similarity=float(sim),
                created_by=str(created_by or ""),
            )
            if rec:
                suggestions.append(rec)
    return suggestions


def create_profiles_for_refs(
    *,
    store: JobStore,
    series_slug: str,
    label_refs: dict[str, Path],
    created_by: str,
    source_job_id: str,
    device: str,
    voice_store_dir: Path | None = None,
) -> dict[str, dict[str, Any]]:
    series_slug = str(series_slug or "").strip()
    if not series_slug or not label_refs:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for label, ref_path in label_refs.items():
        lab = str(label or "").strip()
        if not lab:
            continue
        pid = f"vp_{__import__('secrets').token_hex(8)}"
        try:
            ref_copy = ensure_profile_ref(pid, Path(ref_path), voice_store_dir=voice_store_dir)
        except Exception as ex:
            logger.warning("voice_profile_ref_copy_failed", error=str(ex))
            continue
        emb, provider = compute_embedding(ref_copy, device=device)
        if emb is None:
            logger.warning("voice_profile_embedding_failed", profile_id=pid, label=lab)
            continue
        model_id = _embedding_model_id(provider)
        meta = {
            "ref_path": str(ref_copy),
            "series_slug": series_slug,
            "source_job_id": str(source_job_id or ""),
            "speaker_id": lab,
            "source": "extracted_from_media",
        }
        rec = store.upsert_voice_profile(
            profile_id=pid,
            display_name=str(lab),
            created_by=str(created_by or ""),
            scope="private",
            series_lock=series_slug,
            source_type="extracted_from_media",
            export_allowed=False,
            share_allowed=False,
            reuse_allowed=None,
            expires_at=None,
            embedding_vector=emb,
            embedding_model_id=str(model_id or provider or ""),
            metadata_json=meta,
        )
        out[lab] = {
            "profile_id": str(rec.get("id") or pid),
            "similarity": 1.0,
            "provider": str(provider),
            "ref_path": str(ref_copy),
            "matched_at": float(time.time()),
            "created": True,
        }
        try:
            suggest_similar_profiles(
                store=store,
                profile_id=str(rec.get("id") or pid),
                embedding=emb,
                embedding_model_id=str(model_id or provider or ""),
                series_slug=series_slug,
                created_by=str(created_by or ""),
                allow_global=True,
                threshold=float(getattr(get_settings(), "voice_profile_suggest_threshold", 0.82)),
                voice_store_dir=voice_store_dir,
            )
        except Exception as ex:
            logger.warning("voice_profile_suggest_failed", error=str(ex))
    return out
