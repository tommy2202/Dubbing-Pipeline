from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_write_text, read_json, write_json
from anime_v2.utils.log import logger
from anime_v2.voice_memory.store import VoiceMemoryStore, compute_episode_key


def _now() -> str:
    return __import__("datetime").datetime.now(tz=__import__("datetime").UTC).isoformat()


def _safe_float(x: Any) -> float | None:
    try:
        return float(x)
    except Exception:
        return None


def _cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b:
        return None
    if len(a) != len(b):
        return None
    sa = 0.0
    sb = 0.0
    dot = 0.0
    for x, y in zip(a, b, strict=False):
        fx = float(x)
        fy = float(y)
        dot += fx * fy
        sa += fx * fx
        sb += fy * fy
    if sa <= 0.0 or sb <= 0.0:
        return None
    return float(dot / (math.sqrt(sa) * math.sqrt(sb)))


def _read_glossary_tsv(path: str | Path | None) -> list[tuple[str, str]]:
    if not path:
        return []
    p = Path(str(path))
    if not p.exists() or not p.is_file():
        return []
    out: list[tuple[str, str]] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        a = parts[0].strip()
        b = parts[1].strip()
        if a and b:
            out.append((a, b))
    return out


def _norm(s: str) -> str:
    return " ".join(str(s or "").strip().lower().split())


def _count_term(text: str, term: str) -> int:
    t = _norm(text)
    x = _norm(term)
    if not t or not x:
        return 0
    # Simple substring count (deterministic, offline); avoids regex surprises.
    return int(t.count(x))


def _load_job_segments(job_dir: Path) -> list[dict[str, Any]]:
    job_dir = Path(job_dir)
    translated = job_dir / "translated.json"
    if translated.exists():
        data = read_json(translated, default={})
        segs = data.get("segments") if isinstance(data, dict) else None
        return segs if isinstance(segs, list) else []
    # streaming fallback: aggregate per chunk
    man = job_dir / "stream" / "manifest.json"
    if man.exists():
        m = read_json(man, default={})
        chunks = m.get("chunks") if isinstance(m, dict) else None
        if isinstance(chunks, list):
            out: list[dict[str, Any]] = []
            for ch in chunks:
                if not isinstance(ch, dict):
                    continue
                tj = ch.get("translated_json")
                if not tj:
                    continue
                data = read_json(Path(str(tj)), default={})
                segs = data.get("segments") if isinstance(data, dict) else None
                if isinstance(segs, list):
                    out.extend([s for s in segs if isinstance(s, dict)])
            return out
    return []


def _job_project_name(job_dir: Path) -> str:
    # Prefer job-written project profile artifact if present.
    p = Path(job_dir) / "analysis" / "project_profile.json"
    if p.exists():
        d = read_json(p, default={})
        if isinstance(d, dict):
            nm = str(d.get("name") or "").strip()
            if nm:
                return nm
    return "default"


@dataclass(frozen=True, slots=True)
class DriftSnapshot:
    version: int
    created_at: str
    project: str
    job_id: str
    episode_key: str
    voice: dict[str, Any]
    glossary: dict[str, Any]
    qa: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "created_at": str(self.created_at),
            "project": str(self.project),
            "job_id": str(self.job_id),
            "episode_key": str(self.episode_key),
            "voice": dict(self.voice),
            "glossary": dict(self.glossary),
            "qa": dict(self.qa),
        }


def write_drift_snapshot(
    *,
    job_dir: Path,
    video_path: Path | None,
    voice_memory_dir: Path,
    glossary_path: str | None,
) -> Path:
    """
    Write Output/<job>/analysis/drift_snapshot.json (deterministic, offline).
    """
    job_dir = Path(job_dir)
    (job_dir / "analysis").mkdir(parents=True, exist_ok=True)
    project = _job_project_name(job_dir)
    job_id = str(job_dir.name)
    episode_key = compute_episode_key(audio_hash=None, video_path=Path(video_path) if video_path else None)

    vm = VoiceMemoryStore(Path(voice_memory_dir).resolve())
    chars = vm.list_characters()
    voice_chars: dict[str, Any] = {}
    for c in chars:
        cid = str(c.get("character_id") or "").strip()
        if not cid:
            continue
        emb = vm.load_embedding(cid)
        dp = vm.get_delivery_profile(cid)
        voice_chars[cid] = {
            "display_name": str(c.get("display_name") or ""),
            "voice_mode": str(c.get("voice_mode") or ""),
            "preset_voice_id": str(c.get("preset_voice_id") or ""),
            "delivery_profile": dict(dp) if dp else {},
            "embedding": emb,
        }

    segs = _load_job_segments(job_dir)
    glossary_terms = _read_glossary_tsv(glossary_path)
    g_counts: dict[str, int] = {}
    if glossary_terms and segs:
        # Count target term usage in translated text
        for _src, tgt in glossary_terms:
            g_counts[tgt] = 0
        for s in segs:
            txt = str(s.get("text") or s.get("chosen_text") or "")
            for _src, tgt in glossary_terms:
                g_counts[tgt] += _count_term(txt, tgt)

    qa_summary = {}
    qpath = job_dir / "qa" / "summary.json"
    if qpath.exists():
        d = read_json(qpath, default={})
        if isinstance(d, dict):
            qa_summary = {
                "score": _safe_float(d.get("score")),
                "counts": d.get("counts") if isinstance(d.get("counts"), dict) else {},
                "segments": int(d.get("segments") or 0),
            }

    snap = DriftSnapshot(
        version=1,
        created_at=_now(),
        project=str(project),
        job_id=str(job_id),
        episode_key=str(episode_key),
        voice={"characters": voice_chars},
        glossary={"path": str(glossary_path or ""), "counts": g_counts},
        qa=qa_summary,
    )
    out = job_dir / "analysis" / "drift_snapshot.json"
    write_json(out, snap.to_dict())
    return out


def _reports_root_for(project: str, *, base: Path | None = None) -> Path:
    base = Path(base) if base is not None else (Path.cwd() / "data" / "reports")
    return (base / str(project)).resolve()


def _load_recent_snapshots(reports_root: Path, *, limit: int) -> list[dict[str, Any]]:
    eps = Path(reports_root) / "episodes"
    if not eps.exists():
        return []
    snaps: list[tuple[float, dict[str, Any]]] = []
    for p in eps.glob("*.json"):
        d = read_json(p, default=None)
        if not isinstance(d, dict):
            continue
        ts = p.stat().st_mtime if p.exists() else 0.0
        snaps.append((float(ts), d))
    snaps.sort(key=lambda t: t[0], reverse=True)
    return [d for _, d in snaps[: int(limit)]]


def _md_escape(s: str) -> str:
    return str(s).replace("|", "\\|")


def write_drift_reports(
    *,
    job_dir: Path,
    snapshot_path: Path,
    reports_base: Path | None = None,
    compare_last_n: int = 5,
) -> tuple[Path, Path]:
    """
    Writes:
      - Output/<job>/analysis/drift_report.md
      - data/reports/<project>/season_report.md (regenerated from recent episodes)
    Also archives the job snapshot into data/reports/<project>/episodes/<episode_key>.json
    """
    job_dir = Path(job_dir)
    snap = read_json(snapshot_path, default={})
    if not isinstance(snap, dict):
        raise ValueError("invalid snapshot")
    project = str(snap.get("project") or "default")
    reports_root = _reports_root_for(project, base=reports_base)
    (reports_root / "episodes").mkdir(parents=True, exist_ok=True)

    # Archive snapshot
    ek = str(snap.get("episode_key") or job_dir.name)
    ep_path = reports_root / "episodes" / f"{ek}.json"
    write_json(ep_path, snap)

    # Load recent including current
    recent = _load_recent_snapshots(reports_root, limit=max(1, int(compare_last_n) + 1))

    # Compare current vs previous (most recent distinct before current)
    prev = None
    for d in recent:
        if str(d.get("episode_key") or "") != ek:
            prev = d
            break

    lines: list[str] = []
    lines.append(f"## Drift report for `{_md_escape(str(job_dir.name))}`")
    lines.append("")
    lines.append(f"- **project**: `{_md_escape(project)}`")
    lines.append(f"- **episode_key**: `{_md_escape(ek)}`")
    lines.append(f"- **created_at**: `{_md_escape(str(snap.get('created_at') or ''))}`")
    if prev is None:
        lines.append("")
        lines.append("No previous episode snapshot found for comparison.")
    else:
        lines.append("")
        lines.append(f"Comparing against previous episode `{_md_escape(str(prev.get('episode_key') or ''))}`.")

        # Voice drift
        lines.append("")
        lines.append("### Voice drift (per character)")
        cur_chars = (snap.get("voice") or {}).get("characters") if isinstance(snap.get("voice"), dict) else {}
        prev_chars = (prev.get("voice") or {}).get("characters") if isinstance(prev.get("voice"), dict) else {}
        if not isinstance(cur_chars, dict) or not isinstance(prev_chars, dict):
            lines.append("- (no voice data)")
        else:
            lines.append("| character_id | embed_cosine | voice_mode Δ | preset Δ |")
            lines.append("|---|---:|---|---|")
            for cid, row in sorted(cur_chars.items(), key=lambda kv: str(kv[0])):
                if not isinstance(row, dict):
                    continue
                prow = prev_chars.get(cid)
                emb = row.get("embedding") if isinstance(row.get("embedding"), list) else None
                pemb = prow.get("embedding") if isinstance(prow, dict) and isinstance(prow.get("embedding"), list) else None
                cos = _cosine([float(x) for x in emb] if emb else None, [float(x) for x in pemb] if pemb else None)
                vm0 = str(row.get("voice_mode") or "")
                vm1 = str(prow.get("voice_mode") or "") if isinstance(prow, dict) else ""
                pv0 = str(row.get("preset_voice_id") or "")
                pv1 = str(prow.get("preset_voice_id") or "") if isinstance(prow, dict) else ""
                vm_delta = "" if vm0 == vm1 else f"`{_md_escape(vm1)}` → `{_md_escape(vm0)}`"
                pv_delta = "" if pv0 == pv1 else f"`{_md_escape(pv1)}` → `{_md_escape(pv0)}`"
                cos_s = f"{cos:.3f}" if cos is not None else ""
                lines.append(f"| `{_md_escape(cid)}` | {cos_s} | {vm_delta} | {pv_delta} |")

        # Glossary drift
        lines.append("")
        lines.append("### Glossary usage drift")
        cur_g = (snap.get("glossary") or {}).get("counts") if isinstance(snap.get("glossary"), dict) else {}
        prev_g = (prev.get("glossary") or {}).get("counts") if isinstance(prev.get("glossary"), dict) else {}
        if not isinstance(cur_g, dict) or not isinstance(prev_g, dict):
            lines.append("- (no glossary data)")
        else:
            # show top changed terms (absolute delta)
            deltas: list[tuple[int, str, int, int]] = []
            for term, c0 in cur_g.items():
                try:
                    a = int(c0)
                except Exception:
                    continue
                try:
                    b = int(prev_g.get(term, 0))
                except Exception:
                    b = 0
                dlt = a - b
                if dlt != 0:
                    deltas.append((abs(dlt), str(term), b, a))
            deltas.sort(key=lambda t: (t[0], t[1]), reverse=True)
            if not deltas:
                lines.append("- No detected changes in glossary term counts (vs previous).")
            else:
                lines.append("| term | prev | cur | Δ |")
                lines.append("|---|---:|---:|---:|")
                for _, term, b, a in deltas[:20]:
                    lines.append(f"| `{_md_escape(term)}` | {b} | {a} | {a-b:+d} |")

        # QA drift
        lines.append("")
        lines.append("### QA trend (episode)")
        cur_q = snap.get("qa") if isinstance(snap.get("qa"), dict) else {}
        prev_q = prev.get("qa") if isinstance(prev.get("qa"), dict) else {}
        sc0 = _safe_float(cur_q.get("score")) if isinstance(cur_q, dict) else None
        sc1 = _safe_float(prev_q.get("score")) if isinstance(prev_q, dict) else None
        if sc0 is None or sc1 is None:
            lines.append("- (QA score not available for comparison)")
        else:
            lines.append(f"- **score**: {sc1:.1f} → {sc0:.1f} ({(sc0-sc1):+.1f})")
            c0 = cur_q.get("counts") if isinstance(cur_q.get("counts"), dict) else {}
            c1 = prev_q.get("counts") if isinstance(prev_q.get("counts"), dict) else {}
            if isinstance(c0, dict) and isinstance(c1, dict):
                lines.append(
                    f"- **counts**: fail {int(c1.get('fail',0))}→{int(c0.get('fail',0))}, "
                    f"warn {int(c1.get('warn',0))}→{int(c0.get('warn',0))}"
                )

    drift_md = job_dir / "analysis" / "drift_report.md"
    atomic_write_text(drift_md, "\n".join(lines).rstrip() + "\n", encoding="utf-8")

    # Season report: last N episodes summary (regenerated)
    season_lines: list[str] = []
    season_lines.append(f"## Season drift report — `{_md_escape(project)}`")
    season_lines.append("")
    season_lines.append(f"Latest snapshots (up to {int(compare_last_n)+1}):")
    season_lines.append("")
    season_lines.append("| episode_key | created_at | qa_score | fails | warns |")
    season_lines.append("|---|---|---:|---:|---:|")
    for d in reversed(recent):
        ek2 = str(d.get("episode_key") or "")
        ca = str(d.get("created_at") or "")
        q = d.get("qa") if isinstance(d.get("qa"), dict) else {}
        sc = _safe_float(q.get("score")) if isinstance(q, dict) else None
        counts = q.get("counts") if isinstance(q, dict) and isinstance(q.get("counts"), dict) else {}
        season_lines.append(
            f"| `{_md_escape(ek2)}` | `{_md_escape(ca)}` | {'' if sc is None else f'{sc:.1f}'} | "
            f"{int(counts.get('fail',0) if isinstance(counts, dict) else 0)} | "
            f"{int(counts.get('warn',0) if isinstance(counts, dict) else 0)} |"
        )
    season_md = reports_root / "season_report.md"
    atomic_write_text(season_md, "\n".join(season_lines).rstrip() + "\n", encoding="utf-8")

    logger.info("drift_reports_written", job=str(job_dir.name), project=str(project), episode_key=str(ek))
    return drift_md, season_md

