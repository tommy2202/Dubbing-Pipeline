from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PronunciationEntry:
    term: str
    value: str
    fmt: str  # ipa|phoneme|spelling
    engine: str | None = None
    case_sensitive: bool = False


def _parse_entry(raw: dict[str, Any]) -> PronunciationEntry | None:
    term = str(raw.get("term") or "").strip()
    if not term:
        return None
    fmt = str(raw.get("format") or raw.get("fmt") or "ipa").strip().lower()
    value = str(raw.get("value") or raw.get("ipa_or_phoneme") or raw.get("ipa") or "").strip()
    if isinstance(raw.get("ipa_or_phoneme"), dict):
        data = raw.get("ipa_or_phoneme")
        if isinstance(data, dict):
            fmt = str(data.get("format") or fmt).strip().lower()
            value = str(data.get("value") or value).strip()
    if not value and isinstance(raw.get("ipa_or_phoneme"), str):
        value = str(raw.get("ipa_or_phoneme")).strip()
    if not value:
        return None
    engine = str(raw.get("engine") or raw.get("tts_engine") or "").strip() or None
    case_sensitive = bool(raw.get("case_sensitive") or False)
    if fmt not in {"ipa", "phoneme", "spelling"}:
        fmt = "ipa"
    return PronunciationEntry(term=term, value=value, fmt=fmt, engine=engine, case_sensitive=case_sensitive)


def normalize_pronunciations(rows: list[dict[str, Any]]) -> list[PronunciationEntry]:
    out: list[PronunciationEntry] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        raw = dict(r)
        if isinstance(raw.get("ipa_or_phoneme"), str):
            v = str(raw.get("ipa_or_phoneme") or "").strip()
            if v.startswith("{") and v.endswith("}"):
                try:
                    raw["ipa_or_phoneme"] = json.loads(v)
                except Exception:
                    pass
        entry = _parse_entry(raw)
        if entry is not None:
            out.append(entry)
    return out


def _supports_phonemes(provider: str) -> bool:
    # Current built-ins do not expose phoneme/IPA APIs; keep conservative.
    return False


_VOWELS = set("aeiouAEIOU")


def _spelling_hint(term: str, *, raw: str | None = None) -> str:
    t = str(term or "").strip()
    if not t:
        return ""
    if raw and re.fullmatch(r"[A-Za-z][A-Za-z\-\s']*", raw):
        return raw.strip()
    if re.fullmatch(r"[A-Za-z]+", t):
        out = []
        buf = ""
        for ch in t:
            buf += ch
            if ch in _VOWELS:
                out.append(buf)
                buf = ""
        if buf:
            if out:
                out[-1] = out[-1] + buf
            else:
                out.append(buf)
        if len(out) > 1:
            return "-".join(out)
    return t


def apply_pronunciation(
    text: str,
    entries: list[PronunciationEntry],
    *,
    provider: str,
) -> tuple[str, list[dict[str, Any]]]:
    out = str(text or "")
    warnings: list[dict[str, Any]] = []
    if not out or not entries:
        return out, warnings
    supports = _supports_phonemes(str(provider or "").strip().lower())
    for e in entries:
        if e.engine and str(e.engine).strip().lower() not in {str(provider).lower(), "auto"}:
            continue
        if e.case_sensitive:
            count = out.count(e.term)
            if count <= 0:
                continue
            if supports or e.fmt == "spelling":
                repl = e.value
            else:
                repl = _spelling_hint(e.term, raw=e.value)
                warnings.append({"term": e.term, "format": e.fmt, "provider": provider})
            out = out.replace(e.term, repl)
        else:
            try:
                rx = re.compile(re.escape(e.term), flags=re.IGNORECASE)
            except Exception:
                continue
            matches = list(rx.finditer(out))
            if not matches:
                continue
            if supports or e.fmt == "spelling":
                repl = e.value
            else:
                repl = _spelling_hint(e.term, raw=e.value)
                warnings.append({"term": e.term, "format": e.fmt, "provider": provider})
            out = rx.sub(repl, out)
    return out, warnings
