from __future__ import annotations

import shutil
import sys
from pathlib import Path

from anime_v2.reports.drift import write_drift_reports, write_drift_snapshot
from anime_v2.utils.io import write_json
from anime_v2.voice_memory.store import VoiceMemoryStore


def _tmp_root() -> Path:
    return Path("/workspace/_tmp_drift_reports").resolve()


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    root = _tmp_root()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    # Use a temp voice memory dir (avoid touching repo real data/voice_memory).
    vm_dir = root / "voice_memory"
    vm = VoiceMemoryStore(vm_dir)
    vm.ensure_character(character_id="SPEAKER_01")
    vm.ensure_character(character_id="SPEAKER_02")
    vm.save_embedding("SPEAKER_01", [1.0, 0.0, 0.0], provider="test")
    vm.save_embedding("SPEAKER_02", [0.0, 1.0, 0.0], provider="test")

    # Glossary file
    glossary = root / "glossary.tsv"
    glossary.write_text("鬼殺隊\tDemon Slayer Corps\n", encoding="utf-8")

    # Fake project profile artifact (so reports land under data/reports/<project>)
    project = "example"

    # Episode 1 job
    j1 = root / "Output" / "ep1"
    (j1 / "analysis").mkdir(parents=True, exist_ok=True)
    (j1 / "qa").mkdir(parents=True, exist_ok=True)
    write_json(j1 / "analysis" / "project_profile.json", {"version": 1, "name": project, "profile_hash": "x"})
    write_json(j1 / "qa" / "summary.json", {"version": 1, "enabled": True, "score": 95.0, "counts": {"fail": 0, "warn": 2}, "segments": 10})
    write_json(
        j1 / "translated.json",
        {"src_lang": "ja", "tgt_lang": "en", "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_01", "text": "Demon Slayer Corps!"}]},
    )

    snap1 = write_drift_snapshot(job_dir=j1, video_path=None, voice_memory_dir=vm_dir, glossary_path=str(glossary))
    rep1, season1 = write_drift_reports(job_dir=j1, snapshot_path=snap1, reports_base=(root / "data" / "reports"), compare_last_n=5)
    _assert(rep1.exists(), "episode1 drift_report.md must exist")
    _assert(season1.exists(), "season_report.md must exist after episode1")

    # Episode 2 job (change embedding and QA score)
    vm.save_embedding("SPEAKER_01", [0.95, 0.05, 0.0], provider="test_update")
    vm.set_character_preset("SPEAKER_01", "preset_a")

    j2 = root / "Output" / "ep2"
    (j2 / "analysis").mkdir(parents=True, exist_ok=True)
    (j2 / "qa").mkdir(parents=True, exist_ok=True)
    write_json(j2 / "analysis" / "project_profile.json", {"version": 1, "name": project, "profile_hash": "x"})
    write_json(j2 / "qa" / "summary.json", {"version": 1, "enabled": True, "score": 88.0, "counts": {"fail": 1, "warn": 4}, "segments": 10})
    write_json(
        j2 / "translated.json",
        {"src_lang": "ja", "tgt_lang": "en", "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_01", "text": "No glossary here."}]},
    )

    snap2 = write_drift_snapshot(job_dir=j2, video_path=None, voice_memory_dir=vm_dir, glossary_path=str(glossary))
    rep2, season2 = write_drift_reports(job_dir=j2, snapshot_path=snap2, reports_base=(root / "data" / "reports"), compare_last_n=5)
    _assert(rep2.exists(), "episode2 drift_report.md must exist")
    txt = rep2.read_text(encoding="utf-8", errors="replace")
    _assert("Comparing against previous episode" in txt, "episode2 report must compare against episode1")
    _assert("Voice drift" in txt and "Glossary usage drift" in txt and "QA trend" in txt, "report sections missing")
    _assert(season2.exists(), "season_report.md must exist after episode2")

    print("verify_drift_reports: PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as ex:
        print(f"verify_drift_reports: FAIL: {ex}", file=sys.stderr)
        raise SystemExit(2) from ex

