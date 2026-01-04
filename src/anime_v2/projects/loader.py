from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.io import atomic_write_text, read_json
from anime_v2.utils.log import logger


def _sanitize_project_name(name: str) -> str:
    out = []
    for c in str(name or "").strip():
        if c.isalnum() or c in {"_", "-", "."}:
            out.append(c)
        elif c.isspace():
            out.append("_")
    s = "".join(out).strip("_")
    return s or ""


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    if str(path).lower().endswith(".json"):
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("profile JSON must be an object")
        return data
    try:
        import yaml  # type: ignore

        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            raise ValueError("profile YAML must be an object")
        return data
    except Exception:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("profile must be YAML or JSON object")
        return data


def _sha256_json(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True, slots=True)
class LoadedProjectProfile:
    name: str
    project_dir: Path
    profile_path: Path
    profile_hash: str
    style_guide_path: Path | None
    qa_config: dict[str, Any]
    mix_config: dict[str, Any]
    delivery_config: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "name": self.name,
            "project_dir": str(self.project_dir),
            "profile_path": str(self.profile_path),
            "profile_hash": self.profile_hash,
            "style_guide_path": str(self.style_guide_path) if self.style_guide_path else "",
            "qa": self.qa_config,
            "mix": self.mix_config,
            "delivery": self.delivery_config,
        }


def project_dir(name: str) -> Path | None:
    proj = _sanitize_project_name(name)
    if not proj:
        return None
    s = get_settings()
    p = (Path(s.app_root) / "projects" / proj).resolve()
    return p


def list_project_profiles() -> list[str]:
    s = get_settings()
    root = (Path(s.app_root) / "projects").resolve()
    if not root.exists():
        return []
    out: list[str] = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        if (d / "profile.yaml").exists() or (d / "profile.json").exists():
            out.append(d.name)
    return out


def load_project_profile(name: str) -> LoadedProjectProfile | None:
    proj = _sanitize_project_name(name)
    if not proj:
        return None
    pdir = project_dir(proj)
    if pdir is None or not pdir.exists():
        return None

    profile_path = (pdir / "profile.yaml") if (pdir / "profile.yaml").exists() else (pdir / "profile.json")
    if not profile_path.exists():
        return None

    data = _load_yaml_or_json(profile_path)
    ver = int(data.get("version") or 1)
    if ver != 1:
        raise ValueError(f"Unsupported project profile version: {ver}")

    # Includes: allow per-file overrides, but keep deterministic defaults.
    inc = data.get("include") or data.get("includes") or {}
    if not isinstance(inc, dict):
        inc = {}

    style_rel = str(inc.get("style_guide") or data.get("style_guide") or "style_guide.yaml")
    qa_rel = str(inc.get("qa") or data.get("qa") or "qa.yaml")
    mix_rel = str(inc.get("mix") or data.get("mix") or "mix.yaml")
    delivery_rel = str(inc.get("delivery") or data.get("delivery") or "delivery.yaml")

    style_path = (pdir / style_rel).resolve() if style_rel else None
    if style_path and not style_path.exists():
        # Allow "no style guide" projects; fall back to None.
        style_path = None

    qa_path = (pdir / qa_rel).resolve() if qa_rel else None
    mix_path = (pdir / mix_rel).resolve() if mix_rel else None
    delivery_path = (pdir / delivery_rel).resolve() if delivery_rel else None

    qa_cfg: dict[str, Any] = {}
    mix_cfg: dict[str, Any] = {}
    delivery_cfg: dict[str, Any] = {}
    if qa_path and qa_path.exists():
        qa_cfg = _load_yaml_or_json(qa_path)
    if mix_path and mix_path.exists():
        mix_cfg = _load_yaml_or_json(mix_path)
    if delivery_path and delivery_path.exists():
        delivery_cfg = _load_yaml_or_json(delivery_path)

    # Hash includes content (not just profile.yaml) for stable job provenance.
    hash_payload = {
        "profile": data,
        "qa": qa_cfg,
        "mix": mix_cfg,
        "delivery": delivery_cfg,
        # style guide is already hashed/validated by its loader; we include path only
        "style_guide_path": str(style_path) if style_path else "",
    }
    prof_hash = _sha256_json(hash_payload)

    return LoadedProjectProfile(
        name=proj,
        project_dir=pdir,
        profile_path=profile_path,
        profile_hash=prof_hash,
        style_guide_path=style_path,
        qa_config=qa_cfg if isinstance(qa_cfg, dict) else {},
        mix_config=mix_cfg if isinstance(mix_cfg, dict) else {},
        delivery_config=delivery_cfg if isinstance(delivery_cfg, dict) else {},
    )


def delivery_profiles_from_profile(profile: LoadedProjectProfile) -> dict[str, Any]:
    """
    Returns per-character delivery profile overrides from delivery.yaml (optional).

    Expected shape:
      version: 1
      characters:
        SPEAKER_01:
          rate_mul: 1.05
          pause_style: dramatic
          expressive_strength: 0.7
          preferred_voice_mode: clone
    """
    cfg = profile.delivery_config if isinstance(profile.delivery_config, dict) else {}
    ver = int(cfg.get("version") or 1) if isinstance(cfg, dict) else 1
    if ver != 1:
        return {}
    chars = cfg.get("characters") if isinstance(cfg.get("characters"), dict) else {}
    out: dict[str, Any] = {}
    if isinstance(chars, dict):
        for cid, row in chars.items():
            if not str(cid).strip() or not isinstance(row, dict):
                continue
            d: dict[str, Any] = {}
            if "rate_mul" in row:
                d["rate_mul"] = float(row["rate_mul"])
            if "pause_style" in row:
                d["pause_style"] = str(row["pause_style"] or "").strip().lower()
            if "expressive_strength" in row:
                d["expressive_strength"] = float(row["expressive_strength"])
            if "preferred_voice_mode" in row:
                d["preferred_voice_mode"] = str(row["preferred_voice_mode"] or "").strip().lower()
            out[str(cid).strip()] = d
    return out


def mix_overrides_from_profile(profile: LoadedProjectProfile) -> dict[str, Any]:
    """
    Extract supported mix overrides from profile mix.yaml.
    Only returns keys that are recognized by the existing mixing system.
    """
    cfg = profile.mix_config if isinstance(profile.mix_config, dict) else {}
    ver = int(cfg.get("version") or 1) if isinstance(cfg, dict) else 1
    if ver != 1:
        return {}

    out: dict[str, Any] = {}
    if "mix_profile" in cfg:
        out["mix_profile"] = str(cfg.get("mix_profile") or "").strip().lower()
    if "mix_mode" in cfg:
        out["mix_mode"] = str(cfg.get("mix_mode") or "").strip().lower()
    if "lufs_target" in cfg:
        out["lufs_target"] = float(cfg.get("lufs_target"))
    if "ducking_strength" in cfg:
        out["ducking_strength"] = float(cfg.get("ducking_strength"))
    if "limiter" in cfg:
        out["limiter"] = bool(cfg.get("limiter"))
    return {k: v for k, v in out.items() if v is not None and str(v) != ""}


def qa_thresholds_from_profile(profile: LoadedProjectProfile) -> dict[str, Any]:
    cfg = profile.qa_config if isinstance(profile.qa_config, dict) else {}
    ver = int(cfg.get("version") or 1) if isinstance(cfg, dict) else 1
    if ver != 1:
        return {}
    th = cfg.get("thresholds") or {}
    if not isinstance(th, dict):
        th = {}
    # Keep only known numeric thresholds (others ignored for forward-compat).
    out: dict[str, Any] = {}
    for k in [
        "drift_warn_ratio",
        "drift_fail_ratio",
        "wps_warn",
        "wps_fail",
        "cps_warn",
        "cps_fail",
        "peak_warn",
        "peak_fail",
        "asr_lowconf_warn",
        "asr_lowconf_fail",
    ]:
        if k in th:
            out[k] = float(th[k])
    return out


def write_profile_artifacts(job_dir: Path, profile: LoadedProjectProfile) -> None:
    """
    Persist profile provenance for this job (no secrets).
    """
    job_dir = Path(job_dir)
    (job_dir / "analysis").mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        job_dir / "analysis" / "project_profile.json",
        json.dumps(profile.to_dict(), indent=2, sort_keys=True),
        "utf-8",
    )
    th = qa_thresholds_from_profile(profile)
    if th:
        atomic_write_text(
            job_dir / "analysis" / "qa_profile.json",
            json.dumps({"version": 1, "project": profile.name, "profile_hash": profile.profile_hash, "thresholds": th}, indent=2, sort_keys=True),
            "utf-8",
        )

    dp = delivery_profiles_from_profile(profile)
    if dp:
        atomic_write_text(
            job_dir / "analysis" / "delivery_profiles.json",
            json.dumps({"version": 1, "project": profile.name, "profile_hash": profile.profile_hash, "characters": dp}, indent=2, sort_keys=True),
            "utf-8",
        )


def load_job_qa_profile(job_dir: Path) -> dict[str, Any]:
    """
    Used by QA to load per-job thresholds (written by project profile).
    """
    p = Path(job_dir) / "analysis" / "qa_profile.json"
    data = read_json(p, default={})
    return data if isinstance(data, dict) else {}


def log_profile_applied(*, project: str, profile_hash: str, applied_keys: list[str]) -> None:
    logger.info(
        "project_profile_applied",
        project=str(project),
        profile_hash=str(profile_hash),
        applied_keys=sorted({str(k) for k in applied_keys}),
    )

