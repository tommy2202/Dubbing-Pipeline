"""
Offline timing-aware text fitting utilities.

This module is deterministic and does not require any external services.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def estimate_speaking_seconds(text: str, *, wps: float = 2.7) -> float:
    """
    Rough speech duration estimate.

    - Uses a simple word-per-second heuristic plus small punctuation bonuses.
    - Intended for *relative* fitting, not absolute phoneme timing.
    """
    t = str(text or "").strip()
    if not t:
        return 0.0
    words = len(_WORD_RE.findall(t))
    base = float(words) / max(0.1, float(wps))
    # punctuation adds pauses
    pauses = 0.0
    pauses += 0.10 * t.count(",")
    pauses += 0.25 * (t.count(".") + t.count("!") + t.count("?"))
    pauses += 0.35 * (t.count("...") + t.count("…"))
    return max(0.0, base + pauses)


_CONTRACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bdo not\b"), "don't"),
    (re.compile(r"(?i)\bdoes not\b"), "doesn't"),
    (re.compile(r"(?i)\bdid not\b"), "didn't"),
    (re.compile(r"(?i)\bcan not\b"), "can't"),
    (re.compile(r"(?i)\bwill not\b"), "won't"),
    (re.compile(r"(?i)\bis not\b"), "isn't"),
    (re.compile(r"(?i)\bare not\b"), "aren't"),
    (re.compile(r"(?i)\bwas not\b"), "wasn't"),
    (re.compile(r"(?i)\bwere not\b"), "weren't"),
    (re.compile(r"(?i)\bi am\b"), "I'm"),
    (re.compile(r"(?i)\bi will\b"), "I'll"),
    (re.compile(r"(?i)\bi have\b"), "I've"),
    (re.compile(r"(?i)\bit is\b"), "it's"),
    (re.compile(r"(?i)\bthat is\b"), "that's"),
    (re.compile(r"(?i)\bthere is\b"), "there's"),
    (re.compile(r"(?i)\bwe are\b"), "we're"),
    (re.compile(r"(?i)\bthey are\b"), "they're"),
]

_TIGHTEN: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bin order to\b"), "to"),
    (re.compile(r"(?i)\bas a result of\b"), "because of"),
    (re.compile(r"(?i)\bat this point in time\b"), "now"),
    (re.compile(r"(?i)\bdue to the fact that\b"), "because"),
    (re.compile(r"(?i)\bfor the purpose of\b"), "for"),
]

_FILLER = re.compile(
    r"(?i)\b(really|just|basically|actually|literally|kind of|kinda|sort of|sorta)\b"
)
_LEADING_INTERJ = re.compile(r"(?i)^\s*(well|um+|uh+|like)\s*,?\s+")


def shorten_english(text: str) -> str:
    """
    Deterministic shortening heuristics for English.
    """
    t = str(text or "").strip()
    if not t:
        return ""

    # normalize whitespace
    t = re.sub(r"\s+", " ", t).strip()

    # drop leading interjections
    t2 = _LEADING_INTERJ.sub("", t)
    if t2:
        t = t2

    # apply contractions
    for pat, repl in _CONTRACTIONS:
        t = pat.sub(repl, t)

    # tighten common phrases
    for pat, repl in _TIGHTEN:
        t = pat.sub(repl, t)

    # remove filler adverbs
    t = _FILLER.sub("", t)
    t = re.sub(r"\s+", " ", t).strip()

    # remove duplicated qualifiers (very very -> very)
    t = re.sub(r"(?i)\b(\w+)\s+\1\b", r"\1", t)

    # clean punctuation spacing
    t = re.sub(r"\s+([,!.?])", r"\1", t)
    t = re.sub(r"\(\s+", "(", t)
    t = re.sub(r"\s+\)", ")", t)
    return t.strip()


@dataclass(frozen=True, slots=True)
class FitStats:
    target_s: float
    tolerance: float
    wps: float
    passes: int
    est_before_s: float
    est_after_s: float
    shortened: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_s": self.target_s,
            "tolerance": self.tolerance,
            "wps": self.wps,
            "passes": self.passes,
            "est_before_s": self.est_before_s,
            "est_after_s": self.est_after_s,
            "shortened": self.shortened,
        }


def fit_translation_to_time(
    text: str,
    target_seconds: float,
    *,
    tolerance: float = 0.10,
    max_passes: int = 4,
    wps: float = 2.7,
) -> tuple[str, FitStats]:
    """
    Fit translated text into a time budget using deterministic English heuristics.

    Returns (fitted_text, stats).
    """
    tgt = max(0.0, float(target_seconds))
    tol = max(0.0, float(tolerance))
    t = str(text or "").strip()
    before = estimate_speaking_seconds(t, wps=wps)

    if tgt <= 0.0 or not t:
        stats = FitStats(
            target_s=tgt,
            tolerance=tol,
            wps=float(wps),
            passes=0,
            est_before_s=before,
            est_after_s=before,
            shortened=False,
        )
        return t, stats

    limit = tgt * (1.0 + tol)
    cur = t
    shortened = False
    passes = 0
    for i in range(int(max_passes)):
        est = estimate_speaking_seconds(cur, wps=wps)
        if est <= limit:
            break
        cur2 = shorten_english(cur)
        passes = i + 1
        if cur2 == cur:
            break
        shortened = True
        cur = cur2

    after = estimate_speaking_seconds(cur, wps=wps)
    stats = FitStats(
        target_s=tgt,
        tolerance=tol,
        wps=float(wps),
        passes=passes,
        est_before_s=before,
        est_after_s=after,
        shortened=shortened,
    )
    return cur, stats
