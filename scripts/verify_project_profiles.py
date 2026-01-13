from __future__ import annotations

import json
from pathlib import Path


def main() -> int:
    from dubbing_pipeline.projects.loader import list_project_profiles, load_project_profile

    names = list_project_profiles()
    if not names:
        print("verify_project_profiles: no profiles found (OK)")
        return 0

    errs = 0
    for name in names:
        try:
            prof1 = load_project_profile(name)
            prof2 = load_project_profile(name)
            if prof1 is None or prof2 is None:
                raise RuntimeError("profile failed to load")
            if prof1.profile_hash != prof2.profile_hash:
                raise RuntimeError("profile hash not deterministic across loads")

            # Required layout
            pdir = Path(prof1.project_dir)
            if not (pdir / "profile.yaml").exists() and not (pdir / "profile.json").exists():
                raise RuntimeError("missing profile.yaml/profile.json")
            # Optional includes but common defaults
            # (style_guide.yaml is optional; qa.yaml and mix.yaml are optional but recommended)
            print(
                json.dumps(
                    {
                        "project": prof1.name,
                        "profile_hash": prof1.profile_hash,
                        "project_dir": str(prof1.project_dir),
                        "style_guide_path": str(prof1.style_guide_path) if prof1.style_guide_path else "",
                        "has_qa": bool(prof1.qa_config),
                        "has_mix": bool(prof1.mix_config),
                    },
                    sort_keys=True,
                )
            )
        except Exception as ex:
            errs += 1
            print(f"verify_project_profiles: FAIL project={name}: {ex}")

    if errs:
        return 1
    print("verify_project_profiles: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

