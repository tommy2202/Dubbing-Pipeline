#!/usr/bin/env python3
import os
import sys
import subprocess
from pathlib import Path

MODELS = {
    # Whisper variants
    "whisper-large-v2": ("whisper", "large-v2"),
    # Translation
    "m2m100-418m": ("hf", "facebook/m2m100_418M"),
    # Optional Marian (generic multi->en as placeholder)
    "marian-mul-en": ("hf", "Helsinki-NLP/opus-mt-mul-en"),
    # Wav2Lip checkpoint (official path requires wget)
    "wav2lip-gbn": ("wget", "https://github.com/Rudrabha/Wav2Lip/releases/download/v1.0/wav2lip_gan.pth"),
    # Demucs models are fetched via demucs on first run; allow cache warm-up
}

CACHE = Path(os.environ.get("TRANSFORMERS_CACHE", "/models/hf-cache"))
CACHE.mkdir(parents=True, exist_ok=True)


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def download_hf(model_id: str):
    # Use huggingface-cli to prefetch snapshots
    run([sys.executable, "-m", "huggingface_hub", "download", model_id, "--repo-type", "model", "--local-dir", str(CACHE / model_id.replace("/", "__"))])


def download_whisper(size: str):
    # Trigger whisper to download by a trivial import/load
    code = f"import whisper; whisper.load_model('{size}')"
    run([sys.executable, "-c", code])


def download_wget(url: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["wget", "-O", str(out_dir / Path(url).name), url])


def main():
    base = Path("/models")
    base.mkdir(parents=True, exist_ok=True)

    for name, (kind, ref) in MODELS.items():
        try:
            if kind == "hf":
                download_hf(ref)
            elif kind == "whisper":
                download_whisper(ref)
            elif kind == "wget":
                download_wget(ref, base / "wav2lip")
            else:
                print(f"Skipping unknown kind: {kind}")
        except Exception as ex:
            print(f"Failed to download {name}: {ex}")


if __name__ == "__main__":
    main()
