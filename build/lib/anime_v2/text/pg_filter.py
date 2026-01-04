from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any

from anime_v2.utils.io import atomic_write_text
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class Trigger:
    rule_id: str
    category: str  # profanity|slur|sexual|violence
    replacement: str  # substitute|redact|beep
    term_hash: str
    count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PGPolicy:
    policy_id: str
    profanity_map: dict[str, str] = field(default_factory=dict)  # exact words -> replacement
    slurs: list[str] = field(default_factory=list)  # redacted
    sexual_map: dict[str, str] = field(default_factory=dict)  # phrases -> softer phrase
    violence_map: dict[str, str] = field(default_factory=dict)  # phrases -> softer phrase
    beep_token: str = "[beep]"
    redact_token: str = "[redacted]"
    enable_violence_soften: bool = False


def _hash_term(s: str) -> str:
    return sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _compile_word_re(word: str) -> re.Pattern[str]:
    # word boundary match; deterministic
    return re.compile(rf"(?i)\b{re.escape(word)}\b")


def _compile_phrase_re(phrase: str) -> re.Pattern[str]:
    # phrase match with basic whitespace normalization
    p = r"\s+".join(re.escape(x) for x in phrase.strip().split())
    return re.compile(rf"(?i)\b{p}\b")


def _apply_many(
    text: str,
    *,
    rule_id: str,
    category: str,
    replacement_kind: str,
    patterns: Iterable[tuple[str, re.Pattern[str], str]],
) -> tuple[str, list[Trigger]]:
    out = text
    triggers: list[Trigger] = []
    for key, rx, repl in patterns:
        matches = list(rx.finditer(out))
        if not matches:
            continue
        # Do not log raw term; hash the canonical key
        term_hash = _hash_term(key.lower())
        out = rx.sub(repl, out)
        triggers.append(
            Trigger(
                rule_id=str(rule_id),
                category=str(category),
                replacement=str(replacement_kind),
                term_hash=str(term_hash),
                count=int(len(matches)),
            )
        )
    return out, triggers


def apply_pg_filter(text: str, policy: PGPolicy) -> tuple[str, list[Trigger]]:
    """
    Deterministic offline PG filter.

    Important:
    - Logs and reports MUST NOT include raw matched profanity/slurs.
    - Replacements are conservative and stable.
    """
    if not text:
        return text, []

    out = str(text)
    all_triggers: list[Trigger] = []

    # 1) Slur redaction (strongest)
    slur_pats = [(w, _compile_word_re(w), policy.redact_token) for w in policy.slurs if w.strip()]
    out, t = _apply_many(
        out,
        rule_id="slur_redact",
        category="slur",
        replacement_kind="redact",
        patterns=slur_pats,
    )
    all_triggers.extend(t)

    # 2) Sexual content softening (phrases first)
    sex_pats = [
        (k, _compile_phrase_re(k), v) for k, v in (policy.sexual_map or {}).items() if k.strip()
    ]
    out, t = _apply_many(
        out,
        rule_id="sexual_soften",
        category="sexual",
        replacement_kind="substitute",
        patterns=sex_pats,
    )
    all_triggers.extend(t)

    # 3) Profanity substitution
    prof_pats = [
        (k, _compile_word_re(k), v) for k, v in (policy.profanity_map or {}).items() if k.strip()
    ]
    out, t = _apply_many(
        out,
        rule_id="profanity_substitute",
        category="profanity",
        replacement_kind="substitute",
        patterns=prof_pats,
    )
    all_triggers.extend(t)

    # 4) Optional violence softening
    if bool(policy.enable_violence_soften):
        vio_pats = [
            (k, _compile_phrase_re(k), v)
            for k, v in (policy.violence_map or {}).items()
            if k.strip()
        ]
        out, t = _apply_many(
            out,
            rule_id="violence_soften",
            category="violence",
            replacement_kind="substitute",
            patterns=vio_pats,
        )
        all_triggers.extend(t)

    return out, all_triggers


def built_in_policy(name: str) -> PGPolicy:
    n = str(name or "").strip().lower()
    if n in {"pg", "strict"}:
        return PGPolicy(
            policy_id="pg",
            profanity_map={
                "fuck": "freak",
                "fucking": "freaking",
                "shit": "crud",
                "bitch": "jerk",
                "asshole": "jerk",
                "damn": "darn",
                "bastard": "jerk",
            },
            slurs=[
                # Keep short list; allow user overrides via JSON.
                "retard",
                "kike",
                "nigger",
                "faggot",
            ],
            sexual_map={
                "have sex": "hook up",
                "sleep with": "hook up with",
                "make love": "be together",
                "boobs": "chest",
            },
            enable_violence_soften=True,
            violence_map={
                "kill you": "hurt you",
                "murder": "harm",
                "blood": "injury",
            },
        )

    # Default: pg13
    return PGPolicy(
        policy_id="pg13",
        profanity_map={
            "fuck": "freak",
            "fucking": "freaking",
            "shit": "crap",
            "bitch": "jerk",
            "asshole": "jerk",
            "damn": "dang",
        },
        slurs=[
            "kike",
            "nigger",
            "faggot",
        ],
        sexual_map={
            "have sex": "hook up",
            "sleep with": "hook up with",
            "make love": "be together",
        },
        enable_violence_soften=False,
        violence_map={},
    )


def load_policy_override(path: Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise FileNotFoundError(str(p))
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("policy JSON must be an object")
    return data


def merge_policy(base: PGPolicy, override: dict[str, Any]) -> PGPolicy:
    # deterministic: override replaces/extends known fields
    prof = dict(base.profanity_map)
    slurs = list(base.slurs)
    sex = dict(base.sexual_map)
    vio = dict(base.violence_map)
    beep_token = base.beep_token
    redact_token = base.redact_token
    enable_vio = bool(base.enable_violence_soften)
    policy_id = str(override.get("policy_id") or base.policy_id)

    if isinstance(override.get("profanity_map"), dict):
        for k, v in override["profanity_map"].items():
            if str(k).strip():
                prof[str(k).strip()] = str(v)
    if isinstance(override.get("slurs"), list):
        slurs = [str(x).strip() for x in override["slurs"] if str(x).strip()]
    if isinstance(override.get("sexual_map"), dict):
        for k, v in override["sexual_map"].items():
            if str(k).strip():
                sex[str(k).strip()] = str(v)
    if isinstance(override.get("violence_map"), dict):
        for k, v in override["violence_map"].items():
            if str(k).strip():
                vio[str(k).strip()] = str(v)
    if override.get("beep_token") is not None:
        beep_token = str(override.get("beep_token") or beep_token)
    if override.get("redact_token") is not None:
        redact_token = str(override.get("redact_token") or redact_token)
    if override.get("enable_violence_soften") is not None:
        enable_vio = bool(override.get("enable_violence_soften"))

    return PGPolicy(
        policy_id=policy_id,
        profanity_map=prof,
        slurs=slurs,
        sexual_map=sex,
        violence_map=vio,
        beep_token=beep_token,
        redact_token=redact_token,
        enable_violence_soften=enable_vio,
    )


def resolve_policy(pg: str, pg_policy_path: Path | None = None) -> PGPolicy | None:
    mode = str(pg or "off").strip().lower()
    if mode in {"off", "false", "0", ""}:
        return None
    if mode not in {"pg13", "pg"}:
        mode = "pg13"
    pol = built_in_policy(mode)
    if pg_policy_path is not None:
        try:
            ov = load_policy_override(Path(pg_policy_path))
            pol = merge_policy(pol, ov)
        except Exception:
            logger.exception("pg_policy_override_load_failed", path=str(pg_policy_path))
    return pol


def apply_pg_filter_to_segments(
    segments: list[dict[str, Any]],
    *,
    pg: str,
    pg_policy_path: Path | None,
    report_path: Path | None,
    job_id: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Apply PG filter to segment `text` fields.
    Returns (new_segments, report_dict). If pg=off, returns inputs and a minimal report.
    """
    pol = resolve_policy(pg, pg_policy_path=pg_policy_path)
    report: dict[str, Any] = {
        "version": 1,
        "created_at": time.time(),
        "pg": str(pg or "off"),
        "policy_id": (pol.policy_id if pol else "off"),
        "segments": [],
        "totals": {"segments_changed": 0, "triggers": 0},
    }
    if pol is None:
        if report_path is not None:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(report_path, json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return segments, report

    out: list[dict[str, Any]] = []
    for idx, seg in enumerate(segments):
        seg_id = int(seg.get("segment_id") or (idx + 1))
        txt = str(seg.get("text") or "")
        new_txt, triggers = apply_pg_filter(txt, pol)
        changed = new_txt != txt
        if triggers:
            for tr in triggers:
                logger.info(
                    "pg_filter_trigger",
                    job_id=(job_id or ""),
                    segment_id=int(seg_id),
                    rule_id=tr.rule_id,
                    category=tr.category,
                    replacement=tr.replacement,
                    term_hash=tr.term_hash,
                    count=int(tr.count),
                )
        if changed:
            report["totals"]["segments_changed"] += 1
            report["totals"]["triggers"] += sum(int(t.count) for t in triggers)
        report["segments"].append(
            {
                "segment_id": int(seg_id),
                "changed": bool(changed),
                "trigger_count": int(sum(int(t.count) for t in triggers)),
                "triggers": [t.to_dict() for t in triggers],
            }
        )
        seg2 = dict(seg)
        seg2["text_pre_pg"] = txt if changed else seg2.get("text_pre_pg")
        seg2["text"] = new_txt
        if changed:
            seg2["pg_applied"] = True
            seg2["pg_policy_id"] = pol.policy_id
        out.append(seg2)

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(report_path, json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return out, report

