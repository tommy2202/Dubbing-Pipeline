#!/usr/bin/env python3
import subprocess
import sys
from pathlib import Path

from dubbing_pipeline.config import get_settings

MODELS = {
    # Whisper variants
    "whisper-large": ("whisper", "large"),
    # Translation
    "m2m100-418m": ("hf", "facebook/m2m100_418M"),
    # Optional Marian (generic multi->en baseline)
    "marian-mul-en": ("hf", "Helsinki-NLP/opus-mt-mul-en"),
    # Wav2Lip checkpoints
    "wav2lip-main": (
        "wget",
        "https://github.com/Rudrabha/Wav2Lip/releases/download/" + "v" + "1.0" + "/wav2lip.pth",
    ),
    "wav2lip-gan": (
        "wget",
        "https://github.com/Rudrabha/Wav2Lip/releases/download/" + "v" + "1.0" + "/wav2lip_gan.pth",
    ),
    # Demucs models are fetched via demucs on first run; allow cache warm-up
}

s = get_settings()
_cache = s.transformers_cache or (Path(s.models_dir) / "hf-cache")
CACHE = Path(_cache)
CACHE.mkdir(parents=True, exist_ok=True)


def run(cmd):
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def download_hf(model_id: str):
    # Use huggingface-cli to prefetch snapshots
    run(
        [
            sys.executable,
            "-m",
            "huggingface_hub",
            "download",
            model_id,
            "--repo-type",
            "model",
            "--local-dir",
            str(CACHE / model_id.replace("/", "__")),
        ]
    )


def download_whisper(size: str):
    # Trigger whisper to download by a trivial import/load
    code = f"import whisper; whisper.load_model('{size}')"
    run([sys.executable, "-c", code])


def download_wget(url: str, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    run(["wget", "-O", str(out_dir / Path(url).name), url])


def main():
    base = Path(s.models_dir)
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

    # Optional: clone Wav2Lip repository for inference and face weights
    try:
        repo_dir = base / "Wav2Lip"
        if not repo_dir.exists():
            run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "https://github.com/Rudrabha/Wav2Lip",
                    str(repo_dir),
                ]
            )
        sfd_dir = repo_dir / "face_detection" / "detection" / "sfd"
        sfd_dir.mkdir(parents=True, exist_ok=True)
        s3fd = sfd_dir / "s3fd.pth"
        if not s3fd.exists():
            run(
                [
                    "wget",
                    "-O",
                    str(s3fd),
                    "https://www.adrianbulat.com/downloads/python-fan/s3fd-619a316812.pth",
                ]
            )
    except Exception as ex:
        print(f"Skipping Wav2Lip repo clone or s3fd weights: {ex}")


if __name__ == "__main__":
    main()
