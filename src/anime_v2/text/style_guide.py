from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.io import atomic_write_text
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class AppliedRule:
    rule_id: str
    count: int
    kind: str  # name_map|glossary|honorific|phrase_rule|conflict

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PhraseRule:
    rule_id: str
    pattern: str
    replace: str
    flags: str = ""  # e.g. "i"
    stage: str = "post_translate"
    enabled: bool = True
    order: int = 0


@dataclass(frozen=True, slots=True)
class GlossaryTerm:
    source: str
    target: str
    case_sensitive: bool = False


@dataclass(frozen=True, slots=True)
class HonorificPolicy:
    keep: bool = True
    # Map suffix tokens to replacements. Example: {"-san": "Mr./Ms.", "-chan": ""}
    map: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StyleGuide:
    version: int = 1
    project: str = ""
    name_map: dict[str, str] = field(default_factory=dict)
    glossary_terms: list[GlossaryTerm] = field(default_factory=list)
    honorific_policy: HonorificPolicy = field(default_factory=HonorificPolicy)
    phrase_rules: list[PhraseRule] = field(default_factory=list)
    forbidden_terms: list[str] = field(default_factory=list)
    profanity_policy: str | None = None  # allow|mask|pg13|pg (optional; PG handled elsewhere)


def _sanitize_project_name(name: str) -> str:
    # filesystem-safe deterministic key
    out = []
    for c in str(name or "").strip():
        if c.isalnum() or c in {"_", "-", "."}:
            out.append(c)
        elif c.isspace():
            out.append("_")
    s = "".join(out).strip("_")
    return s or ""


def resolve_style_guide_path(
    *, project: str | None, style_guide_path: Path | None
) -> Path | None:
    if style_guide_path is not None:
        p = Path(style_guide_path).expanduser()
        return p.resolve()
    if project:
        s = get_settings()
        proj = _sanitize_project_name(project)
        if proj:
            cand = (Path(s.app_root) / "projects" / proj / "style_guide.yaml").resolve()
            if cand.exists():
                return cand
            cand2 = (Path(s.app_root) / "projects" / proj / "style_guide.json").resolve()
            if cand2.exists():
                return cand2
    return None


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if str(path).lower().endswith(".json"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("style guide JSON must be an object")
        return data
    # yaml preferred; fallback to json if yaml missing and file looks like json
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise ValueError("style guide YAML must be an object")
        return data
    except Exception:
        # last resort: try JSON
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("style guide must be YAML or JSON object")
        return data


def load_style_guide(path: Path, *, project: str | None = None) -> StyleGuide:
    p = Path(path).resolve()
    data = _load_yaml_or_json(p)
    ver = int(data.get("version") or 1)
    if ver != 1:
        raise ValueError(f"Unsupported style guide version: {ver}")

    name_map = data.get("name_map") or {}
    if not isinstance(name_map, dict):
        name_map = {}
    name_map2 = {str(k): str(v) for k, v in name_map.items() if str(k).strip()}

    glossary_terms: list[GlossaryTerm] = []
    gt = data.get("glossary_terms") or []
    if isinstance(gt, list):
        for it in gt:
            if not isinstance(it, dict):
                continue
            src = str(it.get("source") or "").strip()
            tgt = str(it.get("target") or "").strip()
            if not src or not tgt:
                continue
            glossary_terms.append(
                GlossaryTerm(
                    source=src,
                    target=tgt,
                    case_sensitive=bool(it.get("case_sensitive") or False),
                )
            )

    hp_raw = data.get("honorific_policy") or {}
    keep = True
    hp_map: dict[str, str] = {}
    if isinstance(hp_raw, dict):
        keep = bool(hp_raw.get("keep", True))
        mp = hp_raw.get("map") or {}
        if isinstance(mp, dict):
            hp_map = {str(k): str(v) for k, v in mp.items() if str(k).strip()}

    pr_raw = data.get("phrase_rules") or []
    phrase_rules: list[PhraseRule] = []
    if isinstance(pr_raw, list):
        for i, it in enumerate(pr_raw):
            if not isinstance(it, dict):
                continue
            rid = str(it.get("id") or it.get("rule_id") or f"phrase_{i+1}").strip()
            pat = str(it.get("pattern") or "").strip()
            rep = str(it.get("replace") if "replace" in it else it.get("replacement") or "").strip()
            if not rid or not pat:
                continue
            phrase_rules.append(
                PhraseRule(
                    rule_id=rid,
                    pattern=pat,
                    replace=rep,
                    flags=str(it.get("flags") or ""),
                    stage=str(it.get("stage") or "post_translate"),
                    enabled=bool(it.get("enabled", True)),
                    order=int(it.get("order") or i),
                )
            )

    forb = data.get("forbidden_terms") or []
    forbidden_terms: list[str] = []
    if isinstance(forb, list):
        forbidden_terms = [str(x).strip() for x in forb if str(x).strip()]

    prof_pol = data.get("profanity_policy")
    if prof_pol is not None:
        prof_pol = str(prof_pol).strip().lower() or None

    return StyleGuide(
        version=ver,
        project=str(data.get("project") or project or ""),
        name_map=name_map2,
        glossary_terms=glossary_terms,
        honorific_policy=HonorificPolicy(keep=keep, map=hp_map),
        phrase_rules=phrase_rules,
        forbidden_terms=forbidden_terms,
        profanity_policy=prof_pol,
    )


def _safe_regex(pattern: str) -> None:
    """
    Enforce a conservative, deterministic "safe subset" of regex to reduce ReDoS risk.
    Disallows:
      - lookarounds / inline flags blocks: "(?"
      - backreferences: "\\1", "\\2", ...
    """
    p = str(pattern or "")
    if "(?" in p:
        raise ValueError("regex pattern uses disallowed '(?' construct (lookaround/inline flags)")
    if re.search(r"\\[1-9]", p):
        raise ValueError("regex pattern uses disallowed backreference (\\1..\\9)")
    if len(p) > 300:
        raise ValueError("regex pattern too long")


def _compile_rule(rule: PhraseRule) -> re.Pattern[str]:
    _safe_regex(rule.pattern)
    flags = 0
    f = str(rule.flags or "")
    if "i" in f:
        flags |= re.IGNORECASE
    if "m" in f:
        flags |= re.MULTILINE
    if "s" in f:
        flags |= re.DOTALL
    return re.compile(rule.pattern, flags=flags)


def _apply_replace(text: str, *, rx: re.Pattern[str], repl: str) -> tuple[str, int]:
    # count deterministically
    matches = list(rx.finditer(text))
    if not matches:
        return text, 0
    return rx.sub(repl, text), len(matches)


def apply_style_guide(
    text: str,
    guide: StyleGuide,
    *,
    stage: str = "post_translate",
    max_conflict_steps: int = 8,
) -> tuple[str, list[AppliedRule], dict[str, Any]]:
    """
    Apply project style guide to text deterministically.

    Conflict detection:
      - If applying rules causes the text to repeat a previous state, stop early and record a conflict.
    """
    out = str(text or "")
    applied: list[AppliedRule] = []
    meta: dict[str, Any] = {"conflict": None, "forbidden_hits": []}

    # forbidden term scan (pre)
    forb_hits = []
    for t in guide.forbidden_terms:
        if t and t.lower() in out.lower():
            forb_hits.append(t)
    meta["forbidden_hits"] = forb_hits

    seen: dict[str, str] = {}

    def _mark_seen(rid: str) -> bool:
        h = str(hash(out))
        if h in seen:
            meta["conflict"] = {"kind": "loop", "at_rule": rid, "previous_rule": seen[h]}
            applied.append(AppliedRule(rule_id="conflict_loop", count=1, kind="conflict"))
            return True
        seen[h] = rid
        if len(seen) > int(max_conflict_steps):
            return False
        return False

    # 1) name_map (longest keys first)
    for src in sorted(guide.name_map.keys(), key=lambda s: len(str(s)), reverse=True):
        tgt = str(guide.name_map.get(src) or "")
        if not src:
            continue
        rx = re.compile(rf"(?<!\w){re.escape(src)}(?!\w)")
        out2, n = _apply_replace(out, rx=rx, repl=tgt)
        if n:
            out = out2
            applied.append(AppliedRule(rule_id=f"name_map:{src}", count=n, kind="name_map"))
            if _mark_seen(f"name_map:{src}"):
                return out, applied, meta

    # 2) glossary_terms (target-language enforcement)
    for term in guide.glossary_terms:
        if not term.source:
            continue
        flags = 0 if term.case_sensitive else re.IGNORECASE
        rx = re.compile(re.escape(term.source), flags=flags)
        out2, n = _apply_replace(out, rx=rx, repl=term.target)
        if n:
            out = out2
            applied.append(AppliedRule(rule_id=f"glossary:{term.source}", count=n, kind="glossary"))
            if _mark_seen(f"glossary:{term.source}"):
                return out, applied, meta

    # 3) honorific policy
    if not guide.honorific_policy.keep:
        # drop common -san/-chan/-sama/-kun/-sensei (english text)
        out2, n = _apply_replace(
            out,
            rx=re.compile(r"(?i)\b([A-Za-z][A-Za-z']*)[- ]?(san|chan|sama|kun|sensei)\b"),
            repl=r"\1",
        )
        if n:
            out = out2
            applied.append(AppliedRule(rule_id="honorific:drop", count=n, kind="honorific"))
            if _mark_seen("honorific:drop"):
                return out, applied, meta

    # map suffix tokens (e.g. "-san" -> "Mr./Ms." or "")
    for k, v in (guide.honorific_policy.map or {}).items():
        kk = str(k).strip()
        if not kk:
            continue
        # apply as a token, not regexy: "-san" or "san"
        token = kk.lstrip("-")
        rx = re.compile(rf"(?i)\b([A-Za-z][A-Za-z']*)[- ]?{re.escape(token)}\b")
        out2, n = _apply_replace(out, rx=rx, repl=rf"\1{v}")
        if n:
            out = out2
            applied.append(AppliedRule(rule_id=f"honorific:map:{kk}", count=n, kind="honorific"))
            if _mark_seen(f"honorific:map:{kk}"):
                return out, applied, meta

    # 4) phrase_rules (safe subset, ordered)
    rules = [r for r in guide.phrase_rules if r.enabled and str(r.stage) == str(stage)]
    rules.sort(key=lambda r: (int(r.order), str(r.rule_id)))
    for r in rules:
        try:
            rx = _compile_rule(r)
        except Exception as ex:
            logger.warning("style_guide_rule_rejected", rule_id=r.rule_id, error=str(ex))
            continue
        out2, n = _apply_replace(out, rx=rx, repl=r.replace)
        if n:
            out = out2
            applied.append(AppliedRule(rule_id=f"phrase:{r.rule_id}", count=n, kind="phrase_rule"))
            if _mark_seen(f"phrase:{r.rule_id}"):
                return out, applied, meta

    # forbidden term scan (post)
    forb_hits2 = []
    for t in guide.forbidden_terms:
        if t and t.lower() in out.lower():
            forb_hits2.append(t)
    meta["forbidden_hits"] = forb_hits2

    return out, applied, meta


def apply_style_guide_to_segments(
    segments: list[dict[str, Any]],
    *,
    guide: StyleGuide,
    out_jsonl: Path | None,
    stage: str = "post_translate",
    job_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Apply style guide to each segment's `text`. Optionally write JSONL audit records.
    """
    records: list[str] = []
    out: list[dict[str, Any]] = []
    for i, seg in enumerate(segments):
        sid = int(seg.get("segment_id") or (i + 1))
        before = str(seg.get("text") or "")
        after, applied, meta = apply_style_guide(before, guide, stage=stage)
        seg2 = dict(seg)
        if after != before:
            seg2["text_pre_style_guide"] = before
            seg2["text"] = after
        seg2["style_guide_project"] = guide.project
        seg2["style_guide_applied"] = [a.to_dict() for a in applied]
        if meta.get("conflict"):
            seg2["style_guide_conflict"] = meta.get("conflict")
        if meta.get("forbidden_hits"):
            seg2["style_guide_forbidden_hits"] = meta.get("forbidden_hits")
        out.append(seg2)
        if out_jsonl is not None:
            records.append(
                json.dumps(
                    {
                        "version": 1,
                        "job_id": (job_id or ""),
                        "segment_id": sid,
                        "stage": stage,
                        "project": guide.project,
                        "changed": bool(after != before),
                        "applied_rules": [a.to_dict() for a in applied],
                        "conflict": meta.get("conflict"),
                        "forbidden_hits": meta.get("forbidden_hits") or [],
                    },
                    sort_keys=True,
                )
            )
            for a in applied:
                logger.info(
                    "style_guide_applied",
                    job_id=(job_id or ""),
                    segment_id=sid,
                    rule_id=a.rule_id,
                    kind=a.kind,
                    count=int(a.count),
                )
            if meta.get("conflict"):
                logger.warning(
                    "style_guide_conflict",
                    job_id=(job_id or ""),
                    segment_id=sid,
                    conflict=meta.get("conflict"),
                )
    if out_jsonl is not None:
        out_jsonl.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(out_jsonl, "\n".join(records) + ("\n" if records else ""), encoding="utf-8")
    return out

