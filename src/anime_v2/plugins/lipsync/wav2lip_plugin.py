from __future__ import annotations

import json
import sys
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from anime_v2.config import get_settings
from anime_v2.plugins.lipsync.base import LipSyncPlugin, LipSyncRequest
from anime_v2.plugins.lipsync.preview import preview_lipsync_ranges, write_preview_report
from anime_v2.utils.ffmpeg_safe import extract_audio_mono_16k, ffprobe_duration_seconds, run_ffmpeg
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
            raise FileNotFoundError(
                f"Wav2Lip inference script not found under {repo_dir} (infer.py)"
            )

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

    def _run_wav2lip_once(
        self,
        *,
        paths: Wav2LipPaths,
        face_video: Path,
        audio_wav: Path,
        raw_out: Path,
        face_mode: str,
        bbox: tuple[int, int, int, int] | None,
        device: str,
        timeout_s: int,
        dry_run: bool,
    ) -> Path:
        """
        Run Wav2Lip inference (produces raw_out) and return raw_out.
        """
        raw_out.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            sys.executable,
            str(paths.infer_py),
            "--checkpoint_path",
            str(paths.checkpoint),
            "--face",
            str(face_video),
            "--audio",
            str(audio_wav),
            "--outfile",
            str(raw_out),
        ]

        dev = (device or "auto").lower()
        if dev not in {"auto", "cpu", "cuda"}:
            dev = "auto"
        if dev != "auto":
            cmd += ["--device", dev]

        if face_mode in {"center", "bbox"} and bbox is not None:
            x1, y1, x2, y2 = bbox
            cmd += ["--box", str(x1), str(y1), str(x2), str(y2)]

        logger.info("[v2] lipsync: cmd=%s", " ".join(cmd))
        if dry_run:
            return raw_out

        import subprocess

        try:
            subprocess.run(cmd, cwd=str(paths.repo_dir), check=True, timeout=int(timeout_s))
        except subprocess.TimeoutExpired as ex:
            raise RuntimeError(f"Wav2Lip timed out after {timeout_s}s") from ex
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
                str(face_video),
                "--audio",
                str(audio_wav),
                "--outfile",
                str(raw_out),
            ]
            try:
                subprocess.run(cmd2, cwd=str(paths.repo_dir), check=True, timeout=int(timeout_s))
            except Exception as ex2:
                raise RuntimeError(f"Wav2Lip failed: {ex2}") from ex2

        if not raw_out.exists() or raw_out.stat().st_size == 0:
            raise RuntimeError("Wav2Lip finished but produced no output file.")
        return raw_out

    def _slice_video_segment(
        self, *, src_video: Path, start_s: float, end_s: float, out_mp4: Path
    ) -> Path:
        """
        Re-encode a segment to a consistent H.264 baseline profile so concat works reliably.
        """
        s = get_settings()
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(s.ffmpeg_bin),
            "-y",
            "-ss",
            f"{float(start_s):.3f}",
            "-to",
            f"{float(end_s):.3f}",
            "-i",
            str(src_video),
            "-an",
            "-c:v",
            "libx264",
            "-profile:v",
            "baseline",
            "-level",
            "3.0",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        run_ffmpeg(cmd, timeout_s=600, retries=0, capture=True)
        return out_mp4

    def _mux_video_audio(
        self, *, video_mp4: Path, audio_wav: Path, out_mp4: Path, timeout_s: int
    ) -> Path:
        s = get_settings()
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            str(s.ffmpeg_bin),
            "-y",
            "-i",
            str(video_mp4),
            "-i",
            str(audio_wav),
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
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        run_ffmpeg(cmd, timeout_s=int(timeout_s), retries=0, capture=True)
        return out_mp4

    def _mux_passthrough_full(
        self, *, src_video: Path, audio_wav: Path, out_mp4: Path, timeout_s: int
    ) -> Path:
        """
        Best-effort "no lipsync" output: original video + dubbed audio.
        Tries stream-copy video first; falls back to re-encode if needed.
        """
        s = get_settings()
        out_mp4.parent.mkdir(parents=True, exist_ok=True)
        cmd_copy = [
            str(s.ffmpeg_bin),
            "-y",
            "-i",
            str(src_video),
            "-i",
            str(audio_wav),
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
            "-shortest",
            "-movflags",
            "+faststart",
            str(out_mp4),
        ]
        try:
            run_ffmpeg(cmd_copy, timeout_s=int(timeout_s), retries=0, capture=True)
            return out_mp4
        except Exception:
            cmd_enc = [
                str(s.ffmpeg_bin),
                "-y",
                "-i",
                str(src_video),
                "-i",
                str(audio_wav),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-profile:v",
                "baseline",
                "-level",
                "3.0",
                "-pix_fmt",
                "yuv420p",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ]
            run_ffmpeg(cmd_enc, timeout_s=int(timeout_s), retries=0, capture=True)
            return out_mp4

    def _concat_mp4s(self, *, mp4s: list[Path], out_mp4: Path, timeout_s: int) -> Path:
        s = get_settings()
        out_mp4.parent.mkdir(parents=True, exist_ok=True)

        def esc(p: Path) -> str:
            return p.as_posix().replace("'", r"'\''")

        lst = out_mp4.with_suffix(".concat.txt")
        from anime_v2.utils.io import atomic_write_text

        atomic_write_text(lst, "".join([f"file '{esc(p)}'\n" for p in mp4s]), encoding="utf-8")
        run_ffmpeg(
            [
                str(s.ffmpeg_bin),
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c",
                "copy",
                str(out_mp4),
            ],
            timeout_s=int(timeout_s),
            retries=0,
            capture=True,
        )
        return out_mp4

    def _normalize_ranges(
        self, ranges: list[tuple[float, float]], *, duration_s: float
    ) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        for a, b in ranges:
            try:
                aa = float(a)
                bb = float(b)
            except Exception:
                continue
            aa = max(0.0, min(float(duration_s), aa))
            bb = max(aa, min(float(duration_s), bb))
            if bb - aa < 0.05:
                continue
            out.append((aa, bb))
        out.sort(key=lambda t: t[0])
        merged: list[tuple[float, float]] = []
        for a, b in out:
            if not merged:
                merged.append((a, b))
                continue
            la, lb = merged[-1]
            if a <= lb + 1e-3:
                merged[-1] = (la, max(lb, b))
            else:
                merged.append((a, b))
        return merged

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

        logger.info(
            "[v2] lipsync: running Wav2Lip", repo=str(paths.repo_dir), infer=str(paths.infer_py)
        )

        # Feature J: scene-limited lip-sync
        duration_s = 0.0
        with suppress(Exception):
            duration_s = float(ffprobe_duration_seconds(req.input_video))

        ranges: list[tuple[float, float]] = []
        if bool(req.scene_limited):
            if req.ranges:
                ranges = list(req.ranges)
            else:
                # Auto-detect "good face visibility" ranges (best-effort; offline). Write report for auditability.
                try:
                    rep = preview_lipsync_ranges(
                        video=req.input_video,
                        work_dir=req.work_dir / "preview",
                        sample_every_s=float(req.sample_every_s),
                        max_frames=int(req.max_frames),
                        min_face_ratio=float(req.min_face_ratio),
                        min_range_s=float(req.min_range_s),
                        merge_gap_s=float(req.merge_gap_s),
                    )
                    analysis_dir = req.output_video.parent / "analysis"
                    analysis_dir.mkdir(parents=True, exist_ok=True)
                    write_preview_report(rep, out_path=analysis_dir / "lipsync_preview.json")
                    ranges = [(float(r.start_s), float(r.end_s)) for r in rep.recommended_ranges]
                except Exception as ex:
                    logger.warning(
                        "[v2] lipsync: preview failed; falling back to pass-through (%s)", ex
                    )
                    ranges = []

        if bool(req.scene_limited) and not ranges:
            # No "good face" ranges OR detector unavailable: pass-through full video (no Wav2Lip inference).
            logger.warning(
                "[v2] lipsync: scene-limited enabled but no valid ranges; output will be pass-through."
            )
            if req.dry_run:
                return req.output_video
            with suppress(Exception):
                analysis_dir = req.output_video.parent / "analysis"
                analysis_dir.mkdir(parents=True, exist_ok=True)
                from anime_v2.utils.io import atomic_write_text

                atomic_write_text(
                    analysis_dir / "lipsync_ranges.jsonl",
                    json.dumps(
                        {
                            "range": [0.0, float(duration_s or 0.0)],
                            "mode": "scene_limited",
                            "status": "skipped_no_ranges",
                        },
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            self._mux_passthrough_full(
                src_video=req.input_video,
                audio_wav=req.dubbed_audio_wav,
                out_mp4=req.output_video,
                timeout_s=int(req.timeout_s),
            )
            return req.output_video

        if not bool(req.scene_limited):
            # Full-video mode (legacy behavior)
            raw_out = req.work_dir / "lipsynced.raw.mp4"
            self._run_wav2lip_once(
                paths=paths,
                face_video=req.input_video,
                audio_wav=req.dubbed_audio_wav,
                raw_out=raw_out,
                face_mode=face_mode,
                bbox=bbox,
                device=str(req.device),
                timeout_s=int(req.timeout_s),
                dry_run=bool(req.dry_run),
            )
            if req.dry_run:
                return req.output_video
            self._mux_video_audio(
                video_mp4=raw_out,
                audio_wav=req.dubbed_audio_wav,
                out_mp4=req.output_video,
                timeout_s=int(req.timeout_s),
            )
            with suppress(Exception):
                raw_out.unlink(missing_ok=True)
            return req.output_video

        # Range mode: build pass-through + lipsynced segments and concat.
        if duration_s <= 0.0:
            raise RuntimeError(
                "scene-limited lipsync requires a valid video duration (ffprobe failed)."
            )
        ranges = self._normalize_ranges(ranges, duration_s=float(duration_s))
        if not ranges:
            # Nothing to do; produce pass-through full segment.
            seg_v = self._slice_video_segment(
                src_video=req.input_video,
                start_s=0.0,
                end_s=float(duration_s),
                out_mp4=req.work_dir / "seg_passthrough.mp4",
            )
            seg_a = req.work_dir / "seg_passthrough.wav"
            extract_audio_mono_16k(
                src=req.dubbed_audio_wav,
                dst=seg_a,
                start_s=0.0,
                end_s=float(duration_s),
                timeout_s=120,
            )
            self._mux_video_audio(
                video_mp4=seg_v,
                audio_wav=seg_a,
                out_mp4=req.output_video,
                timeout_s=int(req.timeout_s),
            )
            return req.output_video

        analysis_dir = req.output_video.parent / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        range_log = analysis_dir / "lipsync_ranges.jsonl"

        pieces: list[tuple[float, float, bool]] = []  # (start,end,do_lipsync)
        cur = 0.0
        for a, b in ranges:
            if a > cur + 1e-3:
                pieces.append((cur, a, False))
            pieces.append((a, b, True))
            cur = b
        if cur < float(duration_s) - 1e-3:
            pieces.append((cur, float(duration_s), False))

        mp4s: list[Path] = []
        from anime_v2.utils.io import atomic_write_text

        lines: list[str] = []
        for i, (a, b, do_ls) in enumerate(pieces):
            seg_id = f"{i:03d}"
            seg_dir = req.work_dir / f"seg_{seg_id}"
            seg_dir.mkdir(parents=True, exist_ok=True)
            seg_video = seg_dir / "video.mp4"
            seg_audio = seg_dir / "audio.wav"
            out_seg = seg_dir / "out.mp4"
            try:
                self._slice_video_segment(
                    src_video=req.input_video, start_s=float(a), end_s=float(b), out_mp4=seg_video
                )
                extract_audio_mono_16k(
                    src=req.dubbed_audio_wav,
                    dst=seg_audio,
                    start_s=float(a),
                    end_s=float(b),
                    timeout_s=120,
                )
                if do_ls:
                    raw = seg_dir / "lipsynced.raw.mp4"
                    self._run_wav2lip_once(
                        paths=paths,
                        face_video=seg_video,
                        audio_wav=seg_audio,
                        raw_out=raw,
                        face_mode=face_mode,
                        bbox=bbox,
                        device=str(req.device),
                        timeout_s=int(req.timeout_s),
                        dry_run=bool(req.dry_run),
                    )
                    if req.dry_run:
                        mp4s.append(out_seg)
                        lines.append(
                            json.dumps(
                                {
                                    "range": [float(a), float(b)],
                                    "mode": "lipsync",
                                    "status": "dry_run",
                                },
                                sort_keys=True,
                            )
                        )
                        continue
                    self._mux_video_audio(
                        video_mp4=raw,
                        audio_wav=seg_audio,
                        out_mp4=out_seg,
                        timeout_s=int(req.timeout_s),
                    )
                    with suppress(Exception):
                        raw.unlink(missing_ok=True)
                    lines.append(
                        json.dumps(
                            {"range": [float(a), float(b)], "mode": "lipsync", "status": "ok"},
                            sort_keys=True,
                        )
                    )
                else:
                    # pass-through segment (no Wav2Lip): just mux dubbed audio.
                    if req.dry_run:
                        mp4s.append(out_seg)
                        lines.append(
                            json.dumps(
                                {
                                    "range": [float(a), float(b)],
                                    "mode": "passthrough",
                                    "status": "dry_run",
                                },
                                sort_keys=True,
                            )
                        )
                        continue
                    self._mux_video_audio(
                        video_mp4=seg_video,
                        audio_wav=seg_audio,
                        out_mp4=out_seg,
                        timeout_s=int(req.timeout_s),
                    )
                    lines.append(
                        json.dumps(
                            {"range": [float(a), float(b)], "mode": "passthrough", "status": "ok"},
                            sort_keys=True,
                        )
                    )
                mp4s.append(out_seg)
            except Exception as ex:
                # Skip failed lipsync segments; fall back to passthrough for that range if possible.
                logger.warning(
                    "[v2] lipsync range failed; skipping",
                    start=float(a),
                    end=float(b),
                    error=str(ex),
                )
                lines.append(
                    json.dumps(
                        {
                            "range": [float(a), float(b)],
                            "mode": "lipsync" if do_ls else "passthrough",
                            "status": "fail",
                            "error": str(ex),
                        },
                        sort_keys=True,
                    )
                )
                if req.dry_run:
                    continue
                try:
                    # best-effort passthrough
                    self._mux_video_audio(
                        video_mp4=seg_video,
                        audio_wav=seg_audio,
                        out_mp4=out_seg,
                        timeout_s=int(req.timeout_s),
                    )
                    mp4s.append(out_seg)
                except Exception:
                    continue

        atomic_write_text(range_log, "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        if req.dry_run:
            return req.output_video
        if not mp4s:
            raise RuntimeError("scene-limited lipsync produced no output segments")
        self._concat_mp4s(mp4s=mp4s, out_mp4=req.output_video, timeout_s=int(req.timeout_s))
        return req.output_video


def get_wav2lip_plugin(
    *, wav2lip_dir: Path | None = None, wav2lip_checkpoint: Path | None = None
) -> Wav2LipPlugin:
    return Wav2LipPlugin(wav2lip_dir=wav2lip_dir, checkpoint_path=wav2lip_checkpoint)
