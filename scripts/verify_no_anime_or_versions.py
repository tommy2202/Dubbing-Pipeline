from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Strict: project must not contain historic naming tokens.
#
# Implementation detail: these tokens are constructed from fragments so this script
# itself does not trip the scan it enforces.
_TOK_ANIME = "a" + "nime"
_TOK_V1 = "v" + "1"
_TOK_V2 = "v" + "2"
_TOK_ALPHA = "al" + "pha"

# We intentionally only flag whole-word version tokens to avoid false positives like:
# - model identifiers like "xtts_v2" (underscore keeps it a word char)
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (_TOK_ANIME, re.compile(r"(?i)(?:\b" + _TOK_ANIME + r"\b|\b" + _TOK_ANIME + r"[-_])")),
    (_TOK_V1, re.compile(r"(?i)\b" + _TOK_V1 + r"\b")),
    (_TOK_V2, re.compile(r"(?i)\b" + _TOK_V2 + r"\b")),
    (_TOK_ALPHA, re.compile(r"(?i)\b" + _TOK_ALPHA + r"\b")),
]

ALLOW_FILES = {
    ROOT / "CHANGELOG.md",
}

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
}

# Reasonable text file filter; keep broad on purpose.
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".html",
    ".js",
    ".ts",
    ".css",
    ".sh",
    ".ps1",
    ".env",
}


def _is_text_candidate(p: Path) -> bool:
    if p.name in {".env", ".env.example", ".env.secrets", ".env.secrets.example"}:
        return True
    return p.suffix in TEXT_SUFFIXES


def main() -> int:
    offenders: list[str] = []
    for p in ROOT.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p in ALLOW_FILES:
            continue
        if not _is_text_candidate(p):
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            # Skip binary-ish or non-utf8 files.
            continue

        for name, rx in PATTERNS:
            for m in rx.finditer(txt):
                # Report a small snippet and approximate line number.
                line_no = txt.count("\n", 0, m.start()) + 1
                snippet = txt.splitlines()[line_no - 1].strip()
                offenders.append(f"{p.relative_to(ROOT)}:{line_no}: {name}: {snippet}")

    if offenders:
        print("FAIL: forbidden naming tokens detected:\n", file=sys.stderr)
        for o in offenders[:200]:
            print(f"- {o}", file=sys.stderr)
        if len(offenders) > 200:
            print(f"... and {len(offenders) - 200} more", file=sys.stderr)
        return 2

    print("OK: no forbidden naming tokens found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

