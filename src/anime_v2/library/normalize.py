from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_INT_RE = re.compile(r"(-?\d+)")


def normalize_series_title(title: str) -> str:
    """
    Normalize a user-entered series title for consistent storage/display.

    Rules:
    - trim
    - collapse internal whitespace
    - keep original casing (user-entered)
    """
    t = str(title or "")
    t = t.replace("\x00", "")
    t = _WS_RE.sub(" ", t).strip()
    return t


def series_to_slug(series_title: str) -> str:
    """
    Convert a series title into a stable, URL/path-safe slug.

    - lowercased
    - unicode -> ascii (best-effort)
    - non-alnum collapsed to '-'
    """
    t = normalize_series_title(series_title)
    if not t:
        return ""
    t = unicodedata.normalize("NFKD", t)
    t = t.encode("ascii", "ignore").decode("ascii")
    t = t.lower()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    t = re.sub(r"-{2,}", "-", t).strip("-")
    return t


def parse_int_strict(input_text: object, field_name: str) -> int:
    """
    Parse an integer from a user-entered string with a strict output contract.

    Accepts:
    - 1 / "1" / "01"
    - "Season 1" / "S1" / "Ep 02" (first integer wins)

    Returns:
    - int >= 1

    Raises:
    - ValueError on missing/invalid values.
    """
    if isinstance(input_text, int):
        n = int(input_text)
    else:
        s = str(input_text or "").strip()
        m = _INT_RE.search(s)
        if not m:
            raise ValueError(f"{field_name} must contain a number")
        n = int(m.group(1))
    if n < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return int(n)

