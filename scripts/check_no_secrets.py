from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

ALLOW_TEMPLATE_FILES = {
    ".env.example",
    ".env.secrets.example",
}

PLACEHOLDER_VALUES = {
    "change-me",
    "changeme",
    "CHANGE-ME",
    "CHANGE_ME",
    "example",
    "EXAMPLE",
    "your-token-here",
    "your_password_here",
}

# High-confidence patterns only (avoid noisy generic scans).
PRIVATE_KEY_RE = re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
ENV_SECRET_ASSIGN_RE = re.compile(
    r"(?m)^(JWT_SECRET|SESSION_SECRET|CSRF_SECRET|PASSWORD|TOKEN)\s*=\s*(.+?)\s*$"
)


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]


def _is_probably_binary(path: Path) -> bool:
    # Skip obvious binaries (keeps scan fast and avoids false positives).
    return path.suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp4",
        ".mkv",
        ".wav",
        ".mp3",
        ".flac",
        ".zip",
        ".npy",
        ".npz",
        ".pt",
        ".pth",
        ".onnx",
        ".bin",
    }


def _read_text(path: Path) -> str:
    data = path.read_bytes()
    # Best-effort decode; this is a high-confidence scan, not a parser.
    return data.decode("utf-8", errors="ignore")


def _check_template_placeholders(path: Path, text: str) -> list[str]:
    """
    Templates are allowed to contain env assignments, but they must be placeholders.
    """
    findings: list[str] = []

    for m in ENV_SECRET_ASSIGN_RE.finditer(text):
        key = m.group(1).strip()
        raw_val = (m.group(2) or "").strip()
        # Strip inline comments: FOO=bar  # comment
        val = raw_val.split("#", 1)[0].strip().strip('"').strip("'")

        # Allow obvious placeholders only.
        if val in PLACEHOLDER_VALUES:
            continue

        # Allow common explicit placeholders used elsewhere in this repo.
        if val.lower().startswith("dev-insecure-"):
            continue

        findings.append(f"{path}: non-placeholder value for {key} ({val!r})")

    # Also block private keys even in templates.
    if PRIVATE_KEY_RE.search(text):
        findings.append(f"{path}: private key block detected")

    return findings


def main() -> int:
    try:
        tracked = _tracked_files()
    except Exception as ex:
        print(f"ERROR: failed to run git ls-files: {ex}", file=sys.stderr)
        return 2

    offenders: list[str] = []

    for rel in tracked:
        p = (REPO_ROOT / rel).resolve()
        if not p.exists() or not p.is_file():
            continue

        if _is_probably_binary(p):
            continue

        # Skip gitignored/virtual paths defensively (git ls-files should not include them).
        try:
            txt = _read_text(p)
        except Exception:
            continue

        if p.name in ALLOW_TEMPLATE_FILES:
            offenders.extend(_check_template_placeholders(p, txt))
            continue

        if PRIVATE_KEY_RE.search(txt):
            offenders.append(f"{rel}: private key block detected")

        for m in ENV_SECRET_ASSIGN_RE.finditer(txt):
            key = m.group(1).strip()
            offenders.append(f"{rel}: suspicious secret assignment '{key}='")

    if offenders:
        print("ERROR: High-confidence secret patterns detected in tracked files.", file=sys.stderr)
        for o in sorted(set(offenders)):
            print(f"- {o}", file=sys.stderr)
        print(
            "\nFix: remove secrets from committed files. Use `.env.secrets` (untracked) or CI secrets.",
            file=sys.stderr,
        )
        return 1

    print("check_no_secrets: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
