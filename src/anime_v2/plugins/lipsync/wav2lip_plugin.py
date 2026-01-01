from __future__ import annotations

import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from anime_v2.config import get_settings
from anime_v2.plugins.lipsync.base import LipSyncPlugin, LipSyncRequest
from anime_v2.utils.ffmpeg_safe import run_ffmpeg
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class Wav2LipPaths:
    repo_dir: Path
    infer_py: Path
    checkpoint: Path


def _repo_root() -> Path:
    # src/anime_v2/plugins/lipsync/wav2lip_plugin.py -> src -> repo root
    return Path(__file__).resolve().parents[4]


def _default_repo_candidates() -> list[Path]:
    root = _repo_root()
    s = get_settings()
    return [
        # Tier-3 preferred layout
        (root / "third_party" / "wav2lip").resolve(),
        # Historical v1 layout
        (Path(s.models_dir) / "Wav2Lip").resolve(),
    ]


def _default_ckpt_candidates(repo_dir: Path) -> list[Path]:
    s = get_settings()
    return [
        # Explicit models dir path used by v1
        (Path(s.models_dir) / "wav2lip" / "wav2lip.pth").resolve(),
        # Common Wav2Lip repo layout
        (repo_dir / "checkpoints" / "wav2lip.pth").resolve(),
        (repo_dir / "checkpoints" / "wav2lip_gan.pth").resolve(),
        (repo_dir / "wav2lip.pth").resolve(),
    ]


def _parse_bbox(s: str) -> tuple[int, int, int, int] | None:
    """
    Accepts:
      "x1,y1,x2,y2" or "x1 y1 x2 y2"
    """
    t = " ".join(str(s or "").replace(",", " ").split()).strip()
    if not t:
        return None
    parts = t.split()
    if len(parts) != 4:
        return None
    try:
        x1, y1, x2, y2 = (int(p) for p in parts)
        return x1, y1, x2, y2
    except Exception:
        return None


def _center_box(video: Path) -> tuple[int, int, int, int] | None:
    """
    Best-effort compute a center crop box for face region when face detection is unreliable.
    """
    s = get_settings()
    try:
        import json as _json
        import subprocess

        # ffprobe width/height
        cmd = [
            str(s.ffprobe_bin),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(video),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        data = _json.loads(proc.stdout or "{}")
        streams = data.get("streams", [])
        if not isinstance(streams, list) or not streams:
            return None
        st = streams[0]
        w = int(st.get("width") or 0)
        h = int(st.get("height") or 0)
        if w <= 0 or h <= 0:
            return None
        # square around center (60% of min dim)
        side = int(0.60 * min(w, h))
        cx = w // 2
        cy = h // 2
        x1 = max(0, cx - side // 2)
        y1 = max(0, cy - side // 2)
        x2 = min(w, x1 + side)
        y2 = min(h, y1 + side)
        return int(x1), int(y1), int(x2), int(y2)
    except Exception:
        return None


class Wav2LipPlugin(LipSyncPlugin):
    name = "wav2lip"

    def __init__(
        self,
        *,
        wav2lip_dir: Path | None = None,
        checkpoint_path: Path | None = None,
    ) -> None:
        self._wav2lip_dir = Path(wav2lip_dir).resolve() if wav2lip_dir else None
        self._checkpoint_path = Path(checkpoint_path).resolve() if checkpoint_path else None

    def _resolve_paths(self) -> Wav2LipPaths:
        s = get_settings()

        repo_dir = None
        if self._wav2lip_dir:
            repo_dir = self._wav2lip_dir
        else:
            cfg = getattr(s, "wav2lip_dir", None)
            if cfg:
                with suppress(Exception):
                    repo_dir = Path(str(cfg)).resolve()
        if repo_dir is None:
            for cand in _default_repo_candidates():
                if cand.exists() and cand.is_dir():
                    repo_dir = cand
                    break
        if repo_dir is None:
            raise FileNotFoundError(
                "Wav2Lip repo not found. Provide --wav2lip-dir or place it at "
                "third_party/wav2lip (or MODELS_DIR/Wav2Lip)."
            )

        infer_py = repo_dir / "infer.py"
        if not infer_py.exists():
            # Some forks name it inference.py
            alt = repo_dir / "inference.py"
            if alt.exists():
                infer_py = alt
        if not infer_py.exists():
            raise FileNotFoundError(f"Wav2Lip inference script not found under {repo_dir} (infer.py)")

        ckpt = None
        if self._checkpoint_path:
            ckpt = self._checkpoint_path
        else:
            cfg = getattr(s, "wav2lip_checkpoint", None)
            if cfg:
                with suppress(Exception):
                    ckpt = Path(str(cfg)).resolve()
        if ckpt is None:
            for cand in _default_ckpt_candidates(repo_dir):
                if cand.exists() and cand.is_file():
                    ckpt = cand
                    break
        if ckpt is None or not ckpt.exists():
            raise FileNotFoundError(
                "Wav2Lip checkpoint not found. Provide --wav2lip-checkpoint or set WAV2LIP_CHECKPOINT. "
                "Common path: MODELS_DIR/wav2lip/wav2lip.pth"
            )

        return Wav2LipPaths(repo_dir=repo_dir, infer_py=infer_py, checkpoint=ckpt)

    def is_available(self) -> bool:
        try:
            p = self._resolve_paths()
            return p.repo_dir.exists() and p.infer_py.exists() and p.checkpoint.exists()
        except Exception:
            return False

    def run(self, req: LipSyncRequest) -> Path:
        s = get_settings()
        req.work_dir.mkdir(parents=True, exist_ok=True)
        req.output_video.parent.mkdir(parents=True, exist_ok=True)

        paths = self._resolve_paths()

        face_mode = (req.face_mode or "auto").lower()
        if face_mode not in {"auto", "center", "bbox"}:
            face_mode = "auto"

        bbox = req.bbox
        if face_mode == "bbox" and bbox is None:
            # Optional config/ENV override: LIPSYNC_BOX
            with suppress(Exception):
                cfg = getattr(s, "lipsync_box", None)
                if cfg:
                    bbox = _parse_bbox(str(cfg))
        if face_mode == "center" and bbox is None:
            bbox = _center_box(req.input_video)

        # Wav2Lip produces a video file; we mux audio afterward with ffmpeg.
        raw_out = req.work_dir / "lipsynced.raw.mp4"

        cmd = [
            sys.executable,
            str(paths.infer_py),
            "--checkpoint_path",
            str(paths.checkpoint),
            "--face",
            str(req.input_video),
            "--audio",
            str(req.dubbed_audio_wav),
            "--outfile",
            str(raw_out),
        ]

        # Best-effort device hint (some forks support it).
        dev = (req.device or "auto").lower()
        if dev not in {"auto", "cpu", "cuda"}:
            dev = "auto"
        if dev != "auto":
            cmd += ["--device", dev]

        # Optional face box override (x1 y1 x2 y2) supported by many forks via --box.
        if face_mode in {"center", "bbox"} and bbox is not None:
            x1, y1, x2, y2 = bbox
            cmd += ["--box", str(x1), str(y1), str(x2), str(y2)]

        logger.info("[v2] lipsync: running Wav2Lip", repo=str(paths.repo_dir), infer=str(paths.infer_py))
        logger.info("[v2] lipsync: cmd=%s", " ".join(cmd))

        if req.dry_run:
            # Dry-run: only validate resolution + command construction.
            return req.output_video

        # Run inference. Retry once without optional args if fork rejects them.
        import subprocess

        try:
            subprocess.run(cmd, cwd=str(paths.repo_dir), check=True)
        except subprocess.CalledProcessError as ex:
            raise RuntimeError(f"Wav2Lip failed (exit={ex.returncode}).") from ex
        except Exception:
            # Retry without --device/--box in case of fork mismatch
            cmd2 = [
                sys.executable,
                str(paths.infer_py),
                "--checkpoint_path",
                str(paths.checkpoint),
                "--face",
                str(req.input_video),
                "--audio",
                str(req.dubbed_audio_wav),
                "--outfile",
                str(raw_out),
            ]
            try:
                subprocess.run(cmd2, cwd=str(paths.repo_dir), check=True)
            except Exception as ex2:
                raise RuntimeError(f"Wav2Lip failed: {ex2}") from ex2

        if not raw_out.exists() or raw_out.stat().st_size == 0:
            raise RuntimeError("Wav2Lip finished but produced no output file.")

        # Mux dubbed audio; keep video stream if possible.
        run_ffmpeg(
            [
                str(s.ffmpeg_bin),
                "-y",
                "-i",
                str(raw_out),
                "-i",
                str(req.dubbed_audio_wav),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-shortest",
                str(req.output_video),
            ],
            timeout_s=int(req.timeout_s),
            retries=0,
            capture=True,
        )

        # Best-effort cleanup of raw intermediate
        with suppress(Exception):
            raw_out.unlink(missing_ok=True)

        return req.output_video


def get_wav2lip_plugin(
    *, wav2lip_dir: Path | None = None, wav2lip_checkpoint: Path | None = None
) -> Wav2LipPlugin:
    return Wav2LipPlugin(wav2lip_dir=wav2lip_dir, checkpoint_path=wav2lip_checkpoint)

