from __future__ import annotations

import json
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_write_text, read_json
from anime_v2.utils.log import logger
from anime_v2.utils.subtitles import write_srt, write_vtt

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class SubtitleFormatRules:
    """
    Conservative subtitle formatting defaults.

    These are intentionally "TV-ish" and can be overridden per-project.
    """

    version: int = 1
    max_chars_per_line: int = 42
    max_lines: int = 2
    max_cps: float = 20.0  # chars per second guideline (not strict by default)
    min_duration_s: float = 0.70
    max_duration_s: float = 7.00
    min_gap_to_next_s: float = 0.06  # when extending end time

    @staticmethod
    def from_dict(d: dict[str, Any] | None) -> SubtitleFormatRules:
        if not isinstance(d, dict):
            return SubtitleFormatRules()
        ver = int(d.get("version") or 1)
        if ver != 1:
            return SubtitleFormatRules()
        return SubtitleFormatRules(
            version=1,
            max_chars_per_line=int(d.get("max_chars_per_line", 42)),
            max_lines=int(d.get("max_lines", 2)),
            max_cps=float(d.get("max_cps", 20.0)),
            min_duration_s=float(d.get("min_duration_s", 0.70)),
            max_duration_s=float(d.get("max_duration_s", 7.00)),
            min_gap_to_next_s=float(d.get("min_gap_to_next_s", 0.06)),
        )


@dataclass(frozen=True, slots=True)
class SubtitleFormatStats:
    blocks: int
    changed_blocks: int
    hard_truncations: int
    duration_extended: int
    pre_violations: int
    post_violations: int
    pre_problem_blocks: list[int]
    post_problem_blocks: list[int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _norm_text(text: str) -> str:
    t = str(text or "")
    t = t.replace("\r", " ").replace("\n", " ")
    t = _WS_RE.sub(" ", t).strip()
    return t


def _split_words(text: str) -> list[str]:
    t = _norm_text(text)
    return t.split(" ") if t else []


def _balanced_two_line_split(words: list[str], max_chars: int) -> int | None:
    """
    Return a split index i such that:
      line1 = " ".join(words[:i])
      line2 = " ".join(words[i:])
    and both lines <= max_chars if possible.
    Picks the split that balances line lengths.
    """
    if len(words) < 2:
        return None
    best_i = None
    best_score = 1e18
    for i in range(1, len(words)):
        a = " ".join(words[:i])
        b = " ".join(words[i:])
        la = len(a)
        lb = len(b)
        if la <= max_chars and lb <= max_chars:
            score = max(la, lb) * 1000 + abs(la - lb)
            if score < best_score:
                best_score = score
                best_i = i
    return best_i


def _hard_wrap(text: str, max_chars: int) -> list[str]:
    t = _norm_text(text)
    if not t:
        return [""]
    out: list[str] = []
    i = 0
    while i < len(t):
        out.append(t[i : i + max_chars])
        i += max_chars
    return out


def _wrap_to_lines(text: str, *, max_chars: int, max_lines: int) -> tuple[list[str], bool]:
    """
    Returns (lines, truncated)
    """
    t = _norm_text(text)
    if not t:
        return [""], False
    if max_lines <= 1:
        if len(t) <= max_chars:
            return [t], False
        # hard truncate to fit
        keep = max(1, max_chars - 1)
        return [t[:keep] + "…"], True

    words0 = _split_words(t)
    # Split ultra-long tokens so we can still respect max_chars.
    words: list[str] = []
    for w in words0:
        if len(w) <= max_chars:
            words.append(w)
        else:
            # chunk with hard wrap; treat as independent tokens (no hyphen to avoid altering meaning)
            words.extend(_hard_wrap(w, max_chars))
    if not words:
        return [t], False

    # Try 2-line balanced wrap first (common case).
    if max_lines == 2:
        i = _balanced_two_line_split(words, max_chars)
        if i is not None:
            return [" ".join(words[:i]), " ".join(words[i:])], False

    # Greedy wrap into multiple lines, then clamp to max_lines.
    lines: list[str] = []
    cur: list[str] = []
    for w in words:
        cand = (" ".join(cur + [w])).strip()
        if not cur or len(cand) <= max_chars:
            cur.append(w)
            continue
        lines.append(" ".join(cur))
        cur = [w]
    if cur:
        lines.append(" ".join(cur))

    if len(lines) <= max_lines:
        return lines, False

    # Clamp: merge overflow into last line, truncating if needed.
    kept = lines[: max_lines - 1]
    rest = " ".join(lines[max_lines - 1 :])
    last, truncated = _wrap_to_lines(rest, max_chars=max_chars, max_lines=1)
    return kept + last, True


def _violations_for_block(text: str, dur_s: float, rules: SubtitleFormatRules) -> int:
    t = _norm_text(text)
    lines = t.split("\n") if "\n" in t else [t]
    v = 0
    if len(lines) > int(rules.max_lines):
        v += 1
    if any(len(ln) > int(rules.max_chars_per_line) for ln in lines):
        v += 1
    if dur_s > 0 and (len(t) / dur_s) > float(rules.max_cps):
        v += 1
    return v


def format_subtitle_blocks(
    blocks: list[dict[str, Any]], rules: SubtitleFormatRules
) -> list[dict[str, Any]]:
    """
    Format subtitle blocks in-place style (returns new list).

    Input blocks shape:
      {start: float, end: float, text: str, ...}
    Output preserves start/end (except small best-effort end extension to meet min duration).
    """
    out, _ = format_subtitle_blocks_with_stats(blocks, rules)
    return out


def format_subtitle_blocks_with_stats(
    blocks: list[dict[str, Any]], rules: SubtitleFormatRules
) -> tuple[list[dict[str, Any]], SubtitleFormatStats]:
    blocks_in = [b for b in (blocks if isinstance(blocks, list) else []) if isinstance(b, dict)]
    out: list[dict[str, Any]] = []

    changed_blocks = 0
    hard_trunc = 0
    dur_ext = 0
    pre_v = 0
    post_v = 0
    pre_bad: list[int] = []
    post_bad: list[int] = []

    # use neighbor start for safe end extension
    starts = []
    for b in blocks_in:
        try:
            starts.append(float(b.get("start", 0.0)))
        except Exception:
            starts.append(0.0)

    for i, b in enumerate(blocks_in):
        s0 = float(b.get("start", 0.0))
        e0 = float(b.get("end", s0))
        s = max(0.0, float(s0))
        e = max(s, float(e0))
        txt0 = str(b.get("text") or "")
        dur = max(0.0, e - s)
        v0 = _violations_for_block(txt0, dur, rules)
        pre_v += v0
        if v0:
            pre_bad.append(int(i + 1))

        # duration clamp/extend (best-effort)
        if dur > 0 and dur < float(rules.min_duration_s):
            target = s + float(rules.min_duration_s)
            # do not overlap next start (minus a tiny gap)
            if i + 1 < len(starts):
                target = min(target, max(s, float(starts[i + 1]) - float(rules.min_gap_to_next_s)))
            if target > e:
                e = min(target, s + float(rules.max_duration_s))
                dur_ext += 1

        # wrap text
        lines, truncated = _wrap_to_lines(
            txt0, max_chars=int(rules.max_chars_per_line), max_lines=int(rules.max_lines)
        )
        txt = "\n".join(lines).strip()
        if truncated:
            hard_trunc += 1

        # Duration / CPS constraint (best-effort): if text is too dense, truncate with ellipsis
        dur2 = max(0.0, e - s)
        if dur2 > 0 and float(rules.max_cps) > 0:
            plain = _norm_text(txt)
            max_chars_total = int(math.floor(float(rules.max_cps) * dur2))
            if max_chars_total > 0 and len(plain) > max_chars_total:
                keep = max(1, max_chars_total - 1)
                tightened = plain[:keep] + "…"
                lines2, truncated2 = _wrap_to_lines(
                    tightened,
                    max_chars=int(rules.max_chars_per_line),
                    max_lines=int(rules.max_lines),
                )
                txt = "\n".join(lines2).strip()
                hard_trunc += 1

        b2 = dict(b)
        b2["start"] = s
        b2["end"] = e
        b2["text"] = txt
        if (
            (abs(s - s0) > 1e-6)
            or (abs(e - e0) > 1e-6)
            or (_norm_text(txt) != _norm_text(txt0))
            or ("\n" in txt)
        ):
            changed_blocks += 1
        out.append(b2)

        v1 = _violations_for_block(txt, max(0.0, e - s), rules)
        post_v += v1
        if v1:
            post_bad.append(int(i + 1))

    stats = SubtitleFormatStats(
        blocks=len(out),
        changed_blocks=changed_blocks,
        hard_truncations=hard_trunc,
        duration_extended=dur_ext,
        pre_violations=pre_v,
        post_violations=post_v,
        pre_problem_blocks=pre_bad,
        post_problem_blocks=post_bad,
    )
    return out, stats


def _load_rules_from_job_or_project(
    job_dir: Path, *, project: str | None = None
) -> SubtitleFormatRules:
    # job-local override (future-proof)
    job_dir = Path(job_dir)
    job_cfg = job_dir / "analysis" / "subs_rules.json"
    if job_cfg.exists():
        data = read_json(job_cfg, default={})
        if isinstance(data, dict):
            return SubtitleFormatRules.from_dict(
                data.get("rules") if isinstance(data.get("rules"), dict) else data
            )
    # project profile (optional)
    if project:
        try:
            from anime_v2.projects.loader import load_project_profile

            prof = load_project_profile(str(project))
            if prof is not None:
                subs = prof.mix_config.get("subs") if isinstance(prof.mix_config, dict) else None
                # (mix.yaml isn't a great place, but keep best-effort compatibility)
                if isinstance(subs, dict):
                    return SubtitleFormatRules.from_dict(subs)
        except Exception:
            pass
        try:
            from anime_v2.projects.loader import project_dir as _pdir

            pdir = _pdir(str(project))
            if pdir is not None:
                cand = (Path(pdir) / "subs.yaml").resolve()
                if cand.exists():
                    raw = cand.read_text(encoding="utf-8", errors="replace")
                    try:
                        import yaml  # type: ignore

                        data = yaml.safe_load(raw) or {}
                    except Exception:
                        data = json.loads(raw)
                    if isinstance(data, dict):
                        return SubtitleFormatRules.from_dict(
                            data.get("rules") if isinstance(data.get("rules"), dict) else data
                        )
        except Exception:
            pass
    return SubtitleFormatRules()


def write_formatted_subs_variant(
    *,
    job_dir: Path,
    variant: str,
    blocks: list[dict[str, Any]],
    project: str | None = None,
) -> dict[str, Any]:
    """
    Write formatted SRT+VTT under Output/<job>/subs/<variant>.(srt|vtt)
    and append a formatting summary line to Output/<job>/analysis/subs_formatting.jsonl.
    """
    job_dir = Path(job_dir)
    subs_dir = job_dir / "subs"
    subs_dir.mkdir(parents=True, exist_ok=True)
    rules = _load_rules_from_job_or_project(job_dir, project=project)

    formatted, stats = format_subtitle_blocks_with_stats(blocks, rules)
    # For writers: translate {start,end,text}
    lines = [
        {"start": b["start"], "end": b["end"], "text": str(b.get("text") or "")} for b in formatted
    ]

    srt_path = subs_dir / f"{variant}.srt"
    vtt_path = subs_dir / f"{variant}.vtt"
    write_srt(lines, srt_path)
    write_vtt(lines, vtt_path)

    row = {
        "ts": time.time(),
        "variant": str(variant),
        "rules": asdict(rules),
        "stats": stats.to_dict(),
        "paths": {"srt": str(srt_path), "vtt": str(vtt_path)},
    }
    # jsonl
    analysis_dir = job_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    with (analysis_dir / "subs_formatting.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")

    # summary json (last write wins per variant)
    summ_p = analysis_dir / "subs_formatting_summary.json"
    summ = read_json(summ_p, default={"version": 1, "variants": {}})
    if not isinstance(summ, dict):
        summ = {"version": 1, "variants": {}}
    variants = summ.get("variants", {})
    if not isinstance(variants, dict):
        variants = {}
    variants[str(variant)] = row
    summ["version"] = 1
    summ["variants"] = variants
    atomic_write_text(summ_p, json.dumps(summ, indent=2, sort_keys=True), encoding="utf-8")

    logger.info(
        "subs_formatted",
        variant=str(variant),
        changed_blocks=int(stats.changed_blocks),
        pre_violations=int(stats.pre_violations),
        post_violations=int(stats.post_violations),
    )

    return row
