from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Strict: src/ must not contain legacy marker tokens.
# Docs and tests are allowed to mention historic naming.
_TOK_ANIME_DEC = "anime" + "v2" + "_dec_"
_TOK_ENC = "AN" + "V2" + "ENC"
_TOK_CHAR = "AN" + "V2" + "CHAR"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (_TOK_ANIME_DEC, re.compile(re.escape(_TOK_ANIME_DEC))),
    (_TOK_ENC, re.compile(re.escape(_TOK_ENC))),
    (_TOK_CHAR, re.compile(re.escape(_TOK_CHAR))),
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
    src_root = ROOT / "src"
    if not src_root.exists():
        print("OK: no src/ directory found.")
        return 0
    for p in src_root.rglob("*"):
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

