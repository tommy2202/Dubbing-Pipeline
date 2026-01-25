from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from dubbing_pipeline.text.style_guide import _safe_regex


def normalize_language_pair(src_lang: str, tgt_lang: str) -> str:
    return f"{str(src_lang or '').strip().lower()}->{str(tgt_lang or '').strip().lower()}"


@dataclass(frozen=True, slots=True)
class GlossaryRule:
    rule_id: str
    kind: str  # exact|regex
    source: str = ""
    target: str = ""
    pattern: str = ""
    replace: str = ""
    flags: str = ""
    case_sensitive: bool = False
    priority: int = 0
    order: int = 0
    glossary_id: str | None = None
    glossary_name: str | None = None
    series_slug: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compile_regex(pattern: str, flags: str) -> re.Pattern[str]:
    _safe_regex(pattern)
    f = 0
    fl = str(flags or "")
    if "i" in fl:
        f |= re.IGNORECASE
    if "m" in fl:
        f |= re.MULTILINE
    if "s" in fl:
        f |= re.DOTALL
    return re.compile(pattern, flags=f)


def _apply_replace(text: str, *, rx: re.Pattern[str], repl: str) -> tuple[str, int]:
    matches = list(rx.finditer(text))
    if not matches:
        return text, 0
    return rx.sub(repl, text), len(matches)


def parse_rules_json(
    raw: dict[str, Any] | str,
    *,
    glossary_id: str | None = None,
    glossary_name: str | None = None,
    series_slug: str | None = None,
    base_priority: int = 0,
) -> list[GlossaryRule]:
    if isinstance(raw, str):
        data = json.loads(raw)
    else:
        data = raw
    if not isinstance(data, dict):
        return []
    rules: list[GlossaryRule] = []
    items = data.get("rules")
    if not isinstance(items, list):
        items = []
    # map shorthand: {"map": {"src": "tgt", ...}}
    m = data.get("map")
    if isinstance(m, dict):
        for i, (k, v) in enumerate(m.items()):
            src = str(k or "").strip()
            tgt = str(v or "").strip()
            if not src or not tgt:
                continue
            rules.append(
                GlossaryRule(
                    rule_id=f"{glossary_id or 'gloss'}:map:{i+1}",
                    kind="exact",
                    source=src,
                    target=tgt,
                    case_sensitive=False,
                    priority=int(base_priority),
                    order=1000 + i,
                    glossary_id=glossary_id,
                    glossary_name=glossary_name,
                    series_slug=series_slug,
                )
            )
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        kind = str(it.get("kind") or it.get("type") or "exact").strip().lower()
        rid = str(it.get("id") or it.get("rule_id") or f"rule_{i+1}").strip()
        order = int(it.get("order") or i)
        priority = int(it.get("priority") or base_priority)
        if kind == "regex":
            pat = str(it.get("pattern") or "").strip()
            repl = str(it.get("replace") if "replace" in it else it.get("replacement") or "").strip()
            if not pat:
                continue
            rules.append(
                GlossaryRule(
                    rule_id=f"{glossary_id or 'gloss'}:{rid}",
                    kind="regex",
                    pattern=pat,
                    replace=repl,
                    flags=str(it.get("flags") or ""),
                    case_sensitive=bool(it.get("case_sensitive") or False),
                    priority=priority,
                    order=order,
                    glossary_id=glossary_id,
                    glossary_name=glossary_name,
                    series_slug=series_slug,
                )
            )
        else:
            src = str(it.get("source") or it.get("from") or it.get("term") or "").strip()
            tgt = str(it.get("target") or it.get("to") or it.get("value") or "").strip()
            if not src or not tgt:
                continue
            rules.append(
                GlossaryRule(
                    rule_id=f"{glossary_id or 'gloss'}:{rid}",
                    kind="exact",
                    source=src,
                    target=tgt,
                    case_sensitive=bool(it.get("case_sensitive") or False),
                    priority=priority,
                    order=order,
                    glossary_id=glossary_id,
                    glossary_name=glossary_name,
                    series_slug=series_slug,
                )
            )
    return rules


def parse_tsv_glossary(lines: list[str], *, base_priority: int = 0) -> list[GlossaryRule]:
    rules: list[GlossaryRule] = []
    for i, line in enumerate(lines, 1):
        raw = str(line or "").strip()
        if not raw or raw.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        src = parts[0].strip()
        tgt = parts[1].strip()
        if not src or not tgt:
            continue
        rules.append(
            GlossaryRule(
                rule_id=f"tsv:{i}",
                kind="exact",
                source=src,
                target=tgt,
                case_sensitive=False,
                priority=int(base_priority),
                order=i,
            )
        )
    return rules


def build_rules_from_glossaries(glossaries: list[dict[str, Any]]) -> list[GlossaryRule]:
    rules: list[GlossaryRule] = []
    for g in glossaries:
        if not isinstance(g, dict):
            continue
        raw = g.get("rules_json") or g.get("rules") or {}
        rules.extend(
            parse_rules_json(
                raw,
                glossary_id=str(g.get("id") or ""),
                glossary_name=str(g.get("name") or ""),
                series_slug=str(g.get("series_slug") or "") or None,
                base_priority=int(g.get("priority") or 0),
            )
        )
    return rules


def apply_glossary_to_text(
    text: str,
    rules: list[GlossaryRule],
) -> tuple[str, list[dict[str, Any]]]:
    out = str(text or "")
    if not rules:
        return out, []
    applied: list[dict[str, Any]] = []

    # Deterministic ordering: priority desc, series-specific first, then order, then rule_id
    def _key(r: GlossaryRule) -> tuple[int, int, int, str]:
        series_boost = 1 if r.series_slug else 0
        return (-int(r.priority), -series_boost, int(r.order), str(r.rule_id))

    for rule in sorted(rules, key=_key):
        if rule.kind == "regex":
            try:
                rx = _compile_regex(rule.pattern, rule.flags)
            except Exception:
                continue
            out, count = _apply_replace(out, rx=rx, repl=str(rule.replace or ""))
        else:
            src = str(rule.source or "")
            if not src:
                continue
            if rule.case_sensitive:
                count = out.count(src)
                if count:
                    out = out.replace(src, str(rule.target or ""))
            else:
                try:
                    rx = re.compile(re.escape(src), flags=re.IGNORECASE)
                    out, count = _apply_replace(out, rx=rx, repl=str(rule.target or ""))
                except Exception:
                    count = 0
        if count:
            applied.append(
                {
                    "rule_id": rule.rule_id,
                    "kind": rule.kind,
                    "count": int(count),
                    "glossary_id": rule.glossary_id,
                    "glossary_name": rule.glossary_name,
                    "series_slug": rule.series_slug,
                }
            )
    return out, applied


def apply_glossary_to_segments(
    segments: list[dict[str, Any]],
    rules: list[GlossaryRule],
) -> list[dict[str, Any]]:
    if not segments or not rules:
        return segments
    out_segments: list[dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "")
        after, applied = apply_glossary_to_text(text, rules)
        seg2 = dict(seg)
        seg2["text_post_glossary"] = after
        if after != text:
            seg2["text_pre_glossary"] = text
            seg2["text"] = after
        if applied:
            existing = seg2.get("glossary_applied")
            if isinstance(existing, list):
                seg2["glossary_applied"] = existing + applied
            else:
                seg2["glossary_applied"] = applied
        out_segments.append(seg2)
    return out_segments
