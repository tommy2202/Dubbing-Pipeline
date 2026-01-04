from __future__ import annotations

import argparse
import json
from pathlib import Path


def _build(preset_dir: Path, db_path: Path, embeddings_dir: Path) -> None:
    try:
        import numpy as np  # type: ignore
        from resemblyzer import VoiceEncoder, preprocess_wav  # type: ignore
    except Exception as ex:
        raise SystemExit(
            "Missing dependencies. Install resemblyzer + numpy, e.g.\n"
            "  pip install -e '.[tts]'\n"
            f"Original error: {ex}"
        ) from ex

    preset_dir = preset_dir.resolve()
    db_path = db_path.resolve()
    embeddings_dir = embeddings_dir.resolve()
    embeddings_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    encoder = VoiceEncoder()
    presets: dict[str, dict] = {}

    for sub in sorted(preset_dir.glob("*")):
        if not sub.is_dir():
            continue
        name = sub.name
        wavs = sorted([p for p in sub.rglob("*.wav") if p.is_file()])
        if not wavs:
            continue

        embs = []
        for wav in wavs:
            try:
                w = preprocess_wav(str(wav))
                embs.append(encoder.embed_utterance(w))
            except Exception:
                continue
        if not embs:
            continue

        emb = np.mean(np.stack(embs, axis=0), axis=0).astype(np.float32)
        emb_path = embeddings_dir / f"{name}.npy"
        np.save(str(emb_path), emb)

        # Store embedding path relative to db (portable)
        rel_emb = emb_path.relative_to(db_path.parent)
        presets[name] = {
            "embedding": str(rel_emb).replace("\\", "/"),
            "samples": [str(p.relative_to(db_path.parent)).replace("\\", "/") for p in wavs],
        }

    db = {"version": 1, "presets": presets}
    db_path.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Wrote {db_path} ({len(presets)} presets)")
    print(f"Embeddings in {embeddings_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build preset voice embeddings DB (Resemblyzer).")
    ap.add_argument(
        "--preset-dir", default="voices/presets", help="Directory containing preset subfolders"
    )
    ap.add_argument("--db", default="voices/presets.json", help="Output JSON DB path")
    ap.add_argument(
        "--embeddings-dir", default="voices/embeddings", help="Directory for .npy embeddings"
    )
    args = ap.parse_args()

    _build(Path(args.preset_dir), Path(args.db), Path(args.embeddings_dir))


if __name__ == "__main__":
    main()
