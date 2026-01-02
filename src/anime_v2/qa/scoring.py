from __future__ import annotations

import json
import math
import time
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from anime_v2.review.ops import resolve_job_dir
from anime_v2.utils.io import atomic_write_text, read_json
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class QAIssue:
    check_id: str
    severity: str  # info|warn|fail
    impact: float  # 0..1 (how much to reduce segment score)
    message: str
    suggested_action: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SegmentQA:
    segment_id: int
    start: float
    end: float
    speaker: str
    status: str  # pending|regenerated|locked|unknown
    text: str
    score: float  # 0..100
    issues: list[QAIssue]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["issues"] = [i.to_dict() for i in self.issues]
        return d


def _safe_float(x: Any, default: float | None = None) -> float | None:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _wav_duration_s(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as wf:
            sr = wf.getframerate()
            n = wf.getnframes()
        return float(n) / float(sr) if sr else 0.0
    except Exception:
        return None


def _wav_peak(path: Path) -> float | None:
    """
    Return peak abs sample in [0..1] for PCM int16 WAV. Fast enough for per-job.
    """
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getsampwidth() != 2:
                return None
            n = wf.getnframes()
            # read in chunks
            peak = 0
            chunk = 65536
            while n > 0:
                buf = wf.readframes(min(chunk, n))
                if not buf:
                    break
                n -= len(buf) // 2
                for i in range(0, len(buf), 2):
                    v = int.from_bytes(buf[i : i + 2], "little", signed=True)
                    if abs(v) > peak:
                        peak = abs(v)
            return float(peak) / 32768.0
    except Exception:
        return None


def _word_count(text: str) -> int:
    t = str(text or "").strip()
    if not t:
        return 0
    # crude: words split by whitespace
    return len([w for w in t.split() if w.strip()])


def _char_count(text: str) -> int:
    t = str(text or "")
    # exclude spaces/newlines
    return len([c for c in t if not c.isspace()])


def _severity_rank(sev: str) -> int:
    s = str(sev).lower().strip()
    return {"fail": 3, "warn": 2, "info": 1}.get(s, 0)


def _iter_work_dirs(job_dir: Path) -> Iterable[Path]:
    """
    Best-effort: yield work dirs under Output/<job>/work/<job_id>/...
    """
    w = Path(job_dir) / "work"
    if not w.exists() or not w.is_dir():
        return []
    out = []
    for p in w.iterdir():
        if p.is_dir():
            out.append(p)
    # newest first
    out.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return out


def _load_segments(job_dir: Path) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    """
    Returns (segments, review_by_id).
    """
    job_dir = Path(job_dir)
    review_state = job_dir / "review" / "state.json"
    review_by_id: dict[int, dict[str, Any]] = {}
    if review_state.exists():
        st = read_json(review_state, default={})
        segs = st.get("segments") if isinstance(st, dict) else None
        if isinstance(segs, list):
            for s in segs:
                if not isinstance(s, dict):
                    continue
                sid = int(s.get("segment_id") or 0)
                if sid > 0:
                    review_by_id[sid] = s

    translated = job_dir / "translated.json"
    if translated.exists():
        data = read_json(translated, default={})
        segs = data.get("segments") if isinstance(data, dict) else None
        if isinstance(segs, list):
            out = []
            for i, s in enumerate(segs, 1):
                if not isinstance(s, dict):
                    continue
                ss = dict(s)
                ss.setdefault("segment_id", int(i))
                out.append(ss)
            return out, review_by_id

    # fallback: review segments as source
    if review_by_id:
        return [dict(s) for _, s in sorted(review_by_id.items(), key=lambda kv: kv[0])], review_by_id
    return [], review_by_id


def _load_music_regions(job_dir: Path) -> list[dict[str, Any]]:
    p = Path(job_dir) / "analysis" / "music_regions.json"
    if not p.exists():
        return []
    data = read_json(p, default={})
    regs = data.get("regions") if isinstance(data, dict) else None
    return regs if isinstance(regs, list) else []


def _overlaps(a0: float, a1: float, b0: float, b1: float) -> bool:
    return max(0.0, min(a1, b1) - max(a0, b0)) > 0.0


def _audio_path_for_segment(
    *,
    seg_id: int,
    review_by_id: dict[int, dict[str, Any]],
    tts_manifest: dict[str, Any] | None,
) -> Path | None:
    # 1) review audio (locked or regenerated)
    rec = review_by_id.get(int(seg_id))
    if isinstance(rec, dict):
        p = Path(str(rec.get("audio_path_current") or ""))
        if p.exists():
            return p

    # 2) tts manifest clip list (index aligned)
    if isinstance(tts_manifest, dict):
        clips = tts_manifest.get("clips")
        if isinstance(clips, list) and 0 <= (seg_id - 1) < len(clips):
            p = Path(str(clips[seg_id - 1]))
            if p.exists():
                return p
    return None


def _find_latest_tts_manifest(job_dir: Path) -> dict[str, Any] | None:
    # Prefer base tts_manifest if present
    p0 = Path(job_dir) / "tts_manifest.json"
    if p0.exists():
        data = read_json(p0, default=None)
        return data if isinstance(data, dict) else None
    # Else search newest work dir
    for wd in _iter_work_dirs(job_dir):
        cand = wd / "tts_manifest.json"
        if cand.exists():
            data = read_json(cand, default=None)
            return data if isinstance(data, dict) else None
    return None


def score_job(
    job: str | Path,
    *,
    enabled: bool = True,
    write_outputs: bool = True,
    top_n: int = 20,
    fail_only: bool = False,
) -> dict[str, Any]:
    """
    Compute QA checks for a job directory (Output/<job>/...).
    Writes:
      - qa/segment_scores.jsonl
      - qa/summary.json
      - qa/top_issues.md
    """
    t0 = time.perf_counter()
    job_dir = resolve_job_dir(str(job))
    job_dir.mkdir(parents=True, exist_ok=True)
    qa_dir = job_dir / "qa"
    qa_dir.mkdir(parents=True, exist_ok=True)

    if not enabled:
        summary = {
            "version": 1,
            "enabled": False,
            "job_dir": str(job_dir),
            "score": 100.0,
            "counts": {"info": 0, "warn": 0, "fail": 0},
            "segments": 0,
            "wall_time_s": time.perf_counter() - t0,
        }
        if write_outputs:
            atomic_write_text(qa_dir / "summary.json", json.dumps(summary, indent=2, sort_keys=True), "utf-8")
        return summary

    segments, review_by_id = _load_segments(job_dir)
    music_regions = _load_music_regions(job_dir)
    tts_manifest = _find_latest_tts_manifest(job_dir)

    # Configurable thresholds (safe defaults)
    drift_warn_ratio = 1.10
    drift_fail_ratio = 1.25
    wps_warn = 3.2
    wps_fail = 3.8
    cps_warn = 18.0
    cps_fail = 22.0
    peak_warn = 0.98
    peak_fail = 0.999
    asr_lowconf_warn = -0.80  # avg logprob approx
    asr_lowconf_fail = -1.05

    # Project profile overrides (best-effort; deterministic)
    try:
        from anime_v2.projects.loader import load_job_qa_profile

        prof = load_job_qa_profile(job_dir)
        th = prof.get("thresholds") if isinstance(prof, dict) else None
        if isinstance(th, dict):
            drift_warn_ratio = float(th.get("drift_warn_ratio", drift_warn_ratio))
            drift_fail_ratio = float(th.get("drift_fail_ratio", drift_fail_ratio))
            wps_warn = float(th.get("wps_warn", wps_warn))
            wps_fail = float(th.get("wps_fail", wps_fail))
            cps_warn = float(th.get("cps_warn", cps_warn))
            cps_fail = float(th.get("cps_fail", cps_fail))
            peak_warn = float(th.get("peak_warn", peak_warn))
            peak_fail = float(th.get("peak_fail", peak_fail))
            asr_lowconf_warn = float(th.get("asr_lowconf_warn", asr_lowconf_warn))
            asr_lowconf_fail = float(th.get("asr_lowconf_fail", asr_lowconf_fail))
    except Exception:
        pass

    by_sev = {"info": 0, "warn": 0, "fail": 0}
    seg_rows: list[SegmentQA] = []
    all_issues: list[tuple[int, QAIssue]] = []

    # speaker flip suspicion: compute per window
    speaker_seq: list[tuple[int, float, str]] = []
    for s in segments:
        sid = int(s.get("segment_id") or 0)
        speaker = str(s.get("speaker") or s.get("speaker_id") or s.get("character_id") or "")
        start = float(s.get("start", 0.0))
        if sid > 0 and speaker:
            speaker_seq.append((sid, start, speaker))
    speaker_seq.sort(key=lambda t: t[1])
    flip_flags: dict[int, int] = {}
    for i in range(1, len(speaker_seq)):
        sid_prev, t_prev, sp_prev = speaker_seq[i - 1]
        sid, tcur, sp = speaker_seq[i]
        if sp != sp_prev and (tcur - t_prev) <= 6.0:
            flip_flags[sid] = flip_flags.get(sid, 0) + 1

    for i, seg in enumerate(segments):
        sid = int(seg.get("segment_id") or (i + 1))
        start = float(seg.get("start", 0.0))
        end = float(seg.get("end", start))
        dur = max(0.0, end - start)
        speaker = str(seg.get("speaker") or seg.get("speaker_id") or "SPEAKER_01")
        text = str(seg.get("text") or "")
        status = "unknown"
        if sid in review_by_id:
            status = str(review_by_id[sid].get("status") or "unknown")

        issues: list[QAIssue] = []
        metrics: dict[str, Any] = {"duration_s": dur}

        # speaking rate
        wc = _word_count(text)
        cc = _char_count(text)
        metrics["word_count"] = wc
        metrics["char_count"] = cc
        if dur >= 0.25 and text.strip():
            wps = float(wc) / dur if wc else 0.0
            cps = float(cc) / dur if cc else 0.0
            metrics["wps"] = wps
            metrics["cps"] = cps
            # Prefer WPS if it looks like spaced language
            use_wps = wc >= 2
            if use_wps:
                if wps >= wps_fail:
                    issues.append(
                        QAIssue(
                            check_id="speaking_rate",
                            severity="fail",
                            impact=0.20,
                            message=f"Speaking rate high ({wps:.2f} wps).",
                            suggested_action="Shorten translation, enable timing-fit/pacing, or regenerate this segment.",
                            details={"wps": wps, "threshold": wps_fail},
                        )
                    )
                elif wps >= wps_warn:
                    issues.append(
                        QAIssue(
                            check_id="speaking_rate",
                            severity="warn",
                            impact=0.10,
                            message=f"Speaking rate elevated ({wps:.2f} wps).",
                            suggested_action="Consider timing-fit/pacing or minor text tightening.",
                            details={"wps": wps, "threshold": wps_warn},
                        )
                    )
            else:
                if cps >= cps_fail:
                    issues.append(
                        QAIssue(
                            check_id="speaking_rate",
                            severity="fail",
                            impact=0.20,
                            message=f"Character rate high ({cps:.1f} cps).",
                            suggested_action="Shorten translation or adjust segment pacing.",
                            details={"cps": cps, "threshold": cps_fail},
                        )
                    )
                elif cps >= cps_warn:
                    issues.append(
                        QAIssue(
                            check_id="speaking_rate",
                            severity="warn",
                            impact=0.10,
                            message=f"Character rate elevated ({cps:.1f} cps).",
                            suggested_action="Consider timing-fit/pacing or minor text tightening.",
                            details={"cps": cps, "threshold": cps_warn},
                        )
                    )

        # low ASR confidence (use available logprob/conf; fallback heuristic)
        conf = _safe_float(seg.get("conf") if "conf" in seg else seg.get("logprob"))
        lowconf = bool(seg.get("lowconf")) if "lowconf" in seg else False
        if conf is not None:
            metrics["asr_conf"] = conf
            if conf <= asr_lowconf_fail or lowconf:
                issues.append(
                    QAIssue(
                        check_id="low_asr_confidence",
                        severity="warn" if conf > asr_lowconf_fail else "fail",
                        impact=0.10 if conf > asr_lowconf_fail else 0.18,
                        message=f"Low ASR/MT confidence ({conf:.2f}).",
                        suggested_action="Review transcript/translation for this segment; consider manual edit + regen + lock.",
                        details={"conf": conf, "lowconf": bool(lowconf)},
                    )
                )
        else:
            # heuristic: too short/garbled text
            if text.strip() and sum(c.isalnum() for c in text) / max(1, len(text)) < 0.55:
                issues.append(
                    QAIssue(
                        check_id="low_asr_confidence",
                        severity="info",
                        impact=0.03,
                        message="Text looks noisy/low-signal (no ASR confidence available).",
                        suggested_action="Spot-check this segment in the review loop.",
                        details={},
                    )
                )

        # music overlap warning (informational)
        if music_regions and dur > 0.0 and text.strip():
            for r in music_regions:
                rs = _safe_float(r.get("start"), 0.0) or 0.0
                re = _safe_float(r.get("end"), 0.0) or 0.0
                if _overlaps(start, end, rs, re):
                    issues.append(
                        QAIssue(
                            check_id="music_overlap",
                            severity="info",
                            impact=0.0,
                            message="Segment overlaps detected music region (dialogue may be suppressed).",
                            suggested_action="If false-positive, lower music threshold or disable music-detect.",
                            details={"music_kind": str(r.get("kind") or "music"), "confidence": r.get("confidence")},
                        )
                    )
                    break

        # speaker flip suspicion
        flips = int(flip_flags.get(sid, 0))
        if flips:
            issues.append(
                QAIssue(
                    check_id="speaker_flip_suspicion",
                    severity="warn",
                    impact=0.07,
                    message="Frequent speaker changes in a short window (possible diarization flip).",
                    suggested_action="Check diarization/voice map; consider locking corrected segments.",
                    details={"flip_count": flips},
                )
            )

        # alignment drift + overlap: requires per-seg audio path
        audio_p = _audio_path_for_segment(seg_id=sid, review_by_id=review_by_id, tts_manifest=tts_manifest)
        if audio_p is not None and dur > 0.0:
            adur = _wav_duration_s(audio_p)
            peak = _wav_peak(audio_p)
            metrics["audio_path"] = str(audio_p)
            if adur is not None:
                metrics["audio_duration_s"] = adur
                ratio = adur / dur if dur else 1.0
                if ratio >= drift_fail_ratio:
                    issues.append(
                        QAIssue(
                            check_id="alignment_drift",
                            severity="fail",
                            impact=0.22,
                            message=f"Audio duration exceeds segment window ({adur:.2f}s > {dur:.2f}s).",
                            suggested_action="Enable pacing or regenerate with shorter text; if locked, unlock/regenerate then re-lock.",
                            details={"audio_duration_s": adur, "segment_duration_s": dur, "ratio": ratio},
                        )
                    )
                elif ratio >= drift_warn_ratio:
                    issues.append(
                        QAIssue(
                            check_id="alignment_drift",
                            severity="warn",
                            impact=0.12,
                            message=f"Audio slightly long for window ({adur:.2f}s vs {dur:.2f}s).",
                            suggested_action="Consider pacing or a small text edit for this segment.",
                            details={"audio_duration_s": adur, "segment_duration_s": dur, "ratio": ratio},
                        )
                    )

                # overlap next segment start (if we can infer)
                if i + 1 < len(segments):
                    nstart = float(segments[i + 1].get("start", end))
                    if start + adur > nstart + 0.02:
                        issues.append(
                            QAIssue(
                                check_id="segment_overlap",
                                severity="fail",
                                impact=0.20,
                                message="Segment audio likely overlaps the next segment.",
                                suggested_action="Regenerate with pacing/shorter text; verify segment boundaries.",
                                details={"segment_end_est": start + adur, "next_start": nstart},
                            )
                        )

            # clipping
            if peak is not None:
                metrics["audio_peak"] = peak
                if peak >= peak_fail:
                    issues.append(
                        QAIssue(
                            check_id="audio_clipping",
                            severity="fail",
                            impact=0.18,
                            message="Audio appears clipped (peak too hot).",
                            suggested_action="Lower energy, enable limiter, or regenerate this segment.",
                            details={"peak": peak},
                        )
                    )
                elif peak >= peak_warn:
                    issues.append(
                        QAIssue(
                            check_id="audio_clipping",
                            severity="warn",
                            impact=0.08,
                            message="Audio peak is very high (risk of clipping).",
                            suggested_action="Consider limiter or slightly lower energy/volume.",
                            details={"peak": peak},
                        )
                    )
        else:
            # fallback: can't measure drift/clipping without wav
            metrics["audio_path"] = None

        # compute segment score
        score = 100.0
        for iss in issues:
            score *= max(0.0, 1.0 - float(iss.impact))
        score = max(0.0, min(100.0, score))

        # counts and top list
        for iss in issues:
            sev = str(iss.severity).lower()
            if sev in by_sev:
                by_sev[sev] += 1
            all_issues.append((sid, iss))

        seg_rows.append(
            SegmentQA(
                segment_id=sid,
                start=start,
                end=end,
                speaker=speaker,
                status=status,
                text=text,
                score=float(score),
                issues=issues,
                metrics=metrics,
            )
        )

    # job score: average segment scores, penalize failures
    if seg_rows:
        avg = sum(s.score for s in seg_rows) / float(len(seg_rows))
    else:
        avg = 100.0
    fails = by_sev["fail"]
    job_score = max(0.0, min(100.0, avg - float(fails) * 2.0))

    # top issues
    def issue_key(tup: tuple[int, QAIssue]) -> tuple[int, float]:
        sid, iss = tup
        return (-_severity_rank(iss.severity), float(iss.impact))

    issues_sorted = sorted(all_issues, key=issue_key, reverse=False)
    # reverse sorting trick above is messy; just sort explicitly:
    issues_sorted = sorted(
        all_issues, key=lambda x: (-_severity_rank(x[1].severity), float(x[1].impact)), reverse=False
    )
    if fail_only:
        issues_sorted = [x for x in issues_sorted if str(x[1].severity).lower() == "fail"]
    issues_sorted = issues_sorted[: max(1, int(top_n))]

    summary = {
        "version": 1,
        "enabled": True,
        "job_dir": str(job_dir),
        "score": float(job_score),
        "segment_average_score": float(avg),
        "segments": int(len(seg_rows)),
        "counts": dict(by_sev),
        "top_issues": [
            {"segment_id": sid, **iss.to_dict()} for sid, iss in issues_sorted
        ],
        "wall_time_s": float(time.perf_counter() - t0),
    }

    logger.info(
        "qa_done",
        job_dir=str(job_dir),
        score=float(job_score),
        segments=int(len(seg_rows)),
        info=int(by_sev["info"]),
        warn=int(by_sev["warn"]),
        fail=int(by_sev["fail"]),
        wall_time_s=float(summary["wall_time_s"]),
    )
    for sid, iss in issues_sorted:
        if str(iss.severity).lower() == "fail":
            logger.info("qa_fail", segment_id=int(sid), check_id=iss.check_id)

    if write_outputs:
        seg_path = qa_dir / "segment_scores.jsonl"
        top_md = qa_dir / "top_issues.md"
        summary_path = qa_dir / "summary.json"

        # jsonl segment file
        lines = []
        for s in seg_rows:
            lines.append(json.dumps(s.to_dict(), sort_keys=True))
        atomic_write_text(seg_path, "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

        atomic_write_text(summary_path, json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

        md = ["## Quality report", ""]
        md.append(f"- **Score**: {job_score:.1f}/100")
        md.append(f"- **Segments**: {len(seg_rows)}")
        md.append(f"- **Counts**: fail={by_sev['fail']} warn={by_sev['warn']} info={by_sev['info']}")
        md.append("")
        md.append("## Top issues")
        md.append("")
        if not issues_sorted:
            md.append("_No issues found._")
        else:
            for sid, iss in issues_sorted:
                md.append(
                    f"- **seg {sid}** [{iss.severity}] `{iss.check_id}`: {iss.message}  \n"
                    f"  **Suggested**: {iss.suggested_action}"
                )
        md.append("")
        atomic_write_text(top_md, "\n".join(md) + "\n", encoding="utf-8")

    return summary

