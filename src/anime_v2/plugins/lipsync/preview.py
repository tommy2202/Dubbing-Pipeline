from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from anime_v2.config import get_settings
from anime_v2.utils.ffmpeg_safe import ffprobe_duration_seconds, run_ffmpeg
from anime_v2.utils.io import atomic_write_text
from anime_v2.utils.log import logger


@dataclass(frozen=True, slots=True)
class FaceSample:
    t_s: float
    face_found: bool
    face_count: int | None = None
    method: str = "none"


@dataclass(frozen=True, slots=True)
class RecommendedRange:
    start_s: float
    end_s: float
    face_ratio: float
    reason: str


@dataclass(frozen=True, slots=True)
class LipSyncPreviewReport:
    version: int
    video: str
    duration_s: float
    sample_every_s: float
    method: str
    samples: list[FaceSample]
    face_ratio_overall: float
    recommended_ranges: list[RecommendedRange]
    suggested_ranges: list[RecommendedRange]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": int(self.version),
            "video": str(self.video),
            "duration_s": float(self.duration_s),
            "sample_every_s": float(self.sample_every_s),
            "method": str(self.method),
            "face_ratio_overall": float(self.face_ratio_overall),
            "recommended_ranges": [asdict(r) for r in self.recommended_ranges],
            "suggested_ranges": [asdict(r) for r in self.suggested_ranges],
            "warnings": list(self.warnings),
            # Keep samples last (can be large)
            "samples": [asdict(s) for s in self.samples],
        }


def _try_import_cv2() -> Any | None:
    try:
        import cv2  # type: ignore

        return cv2
    except Exception:
        return None


def _extract_frames(
    *,
    video: Path,
    frames_dir: Path,
    sample_every_s: float,
    max_frames: int,
    scale_width: int = 320,
) -> tuple[float, float]:
    """
    Extract frames at ~1/sample_every_s FPS to frames_dir/frame_%06d.jpg.
    Returns (duration_s, effective_sample_every_s).
    """
    s = get_settings()
    duration = float(ffprobe_duration_seconds(video))
    if duration <= 0:
        return 0.0, float(sample_every_s)

    # Clamp extraction count to avoid huge jobs on long videos.
    sample_every_s = float(max(0.10, sample_every_s))
    if int(max_frames) > 0:
        est = int(duration / sample_every_s) + 1
        if est > int(max_frames):
            sample_every_s = float(duration / float(max_frames))

    fps = 1.0 / float(sample_every_s)
    frames_dir.mkdir(parents=True, exist_ok=True)
    out_pat = frames_dir / "frame_%06d.jpg"
    vf = f"fps={fps:.6f},scale={int(scale_width)}:-1"
    argv = [
        str(s.ffmpeg_bin),
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        "-q:v",
        "4",
        str(out_pat),
    ]
    run_ffmpeg(argv, timeout_s=600, retries=0, capture=True)
    return duration, float(sample_every_s)


def _detect_faces_opencv(frames_dir: Path) -> tuple[list[FaceSample], str, list[str]]:
    """
    Returns (samples, method, warnings). Each sample corresponds to extracted frames in order.
    """
    cv2 = _try_import_cv2()
    if cv2 is None:
        return (
            [],
            "none",
            [
                "opencv-python not installed; face detection unavailable (preview will be heuristic-only)."
            ],
        )

    try:
        cascade_path = (
            Path(str(getattr(cv2.data, "haarcascades", ""))) / "haarcascade_frontalface_default.xml"
        )
        if not cascade_path.exists():
            return [], "none", ["OpenCV haar cascade not found; face detection unavailable."]
        cascade = cv2.CascadeClassifier(str(cascade_path))
    except Exception as ex:
        return [], "none", [f"OpenCV face detector init failed: {ex}"]

    frames = sorted(frames_dir.glob("frame_*.jpg"))
    if not frames:
        return [], "none", ["No frames extracted (ffmpeg produced none)."]

    out: list[FaceSample] = []
    for idx, fp in enumerate(frames):
        try:
            img = cv2.imread(str(fp))
            if img is None:
                out.append(
                    FaceSample(
                        t_s=float(idx), face_found=False, face_count=None, method="opencv_haar"
                    )
                )
                continue
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
            )
            n = int(len(faces)) if faces is not None else 0
            out.append(
                FaceSample(t_s=float(idx), face_found=(n > 0), face_count=n, method="opencv_haar")
            )
        except Exception:
            out.append(
                FaceSample(t_s=float(idx), face_found=False, face_count=None, method="opencv_haar")
            )
    return out, "opencv_haar", []


def _recommend_ranges_from_samples(
    *,
    duration_s: float,
    effective_sample_every_s: float,
    samples: list[FaceSample],
    min_face_ratio: float,
    min_range_s: float,
    merge_gap_s: float,
) -> list[RecommendedRange]:
    if duration_s <= 0 or not samples:
        return []

    # Convert sample indices to timestamps (idx * sample_every_s).
    face_flags: list[tuple[float, bool]] = []
    for s in samples:
        face_flags.append((float(s.t_s) * float(effective_sample_every_s), bool(s.face_found)))

    ranges: list[tuple[float, float, int, int]] = []  # (start,end,face_yes,face_total)
    cur_start: float | None = None
    face_yes = 0
    face_total = 0
    last_t = 0.0
    gap_allow = float(max(0.0, merge_gap_s))
    for t, has_face in face_flags:
        t = float(t)
        if cur_start is None:
            if has_face:
                cur_start = t
                face_yes = 1
                face_total = 1
            last_t = t
            continue

        # In a range: decide whether to keep extending.
        dt = t - last_t
        last_t = t
        if dt > (float(effective_sample_every_s) + gap_allow) and not has_face:
            # close range
            end = float(min(duration_s, last_t + float(effective_sample_every_s)))
            ranges.append((float(cur_start), end, int(face_yes), int(face_total)))
            cur_start = None
            face_yes = 0
            face_total = 0
            continue

        face_total += 1
        if has_face:
            face_yes += 1
        # Keep the range open even if a few frames miss (gap smoothing)

    if cur_start is not None:
        end = float(min(duration_s, last_t + float(effective_sample_every_s)))
        ranges.append((float(cur_start), end, int(face_yes), int(face_total)))

    out: list[RecommendedRange] = []
    for a, b, yes, total in ranges:
        dur = max(0.0, float(b) - float(a))
        if dur < float(min_range_s):
            continue
        ratio = (float(yes) / float(total)) if total > 0 else 0.0
        if ratio < float(min_face_ratio):
            continue
        out.append(
            RecommendedRange(
                start_s=float(max(0.0, a)),
                end_s=float(min(duration_s, b)),
                face_ratio=float(ratio),
                reason="face_visible",
            )
        )
    return out


def preview_lipsync_ranges(
    *,
    video: Path,
    work_dir: Path,
    sample_every_s: float = 0.5,
    max_frames: int = 600,
    min_face_ratio: float = 0.60,
    min_range_s: float = 2.0,
    merge_gap_s: float = 0.6,
) -> LipSyncPreviewReport:
    """
    Offline preview: sample frames and estimate where face visibility is good enough for scene-limited lip-sync.
    Always returns a report (even when face detection isn't available).
    """
    video = Path(video).resolve()
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    warnings: list[str] = []
    duration_s = 0.0
    eff_sample_every_s = float(sample_every_s)
    try:
        frames_dir = work_dir / "frames"
        duration_s, eff_sample_every_s = _extract_frames(
            video=video,
            frames_dir=frames_dir,
            sample_every_s=float(sample_every_s),
            max_frames=int(max_frames),
        )
    except Exception as ex:
        warnings.append(f"Frame extraction failed: {ex}")

    samples: list[FaceSample] = []
    method = "none"
    if duration_s > 0.0:
        try:
            samples, method, w2 = _detect_faces_opencv(work_dir / "frames")
            warnings.extend(w2)
        except Exception as ex:
            warnings.append(f"Face detection failed: {ex}")
            samples = []
            method = "none"

    if duration_s <= 0.0:
        warnings.append("Could not determine duration; no preview ranges computed.")
    if method == "none":
        warnings.append(
            "No face detector available; cannot recommend face-verified ranges. Scene-limited lip-sync will skip."
        )

    # If we couldn't detect faces, recommend the full duration (so scene-limited mode degenerates to pass-through).
    recommended: list[RecommendedRange] = []
    if duration_s > 0.0 and method != "none" and samples:
        recommended = _recommend_ranges_from_samples(
            duration_s=float(duration_s),
            effective_sample_every_s=float(eff_sample_every_s),
            samples=samples,
            min_face_ratio=float(min_face_ratio),
            min_range_s=float(min_range_s),
            merge_gap_s=float(merge_gap_s),
        )

    suggested: list[RecommendedRange] = []
    if duration_s > 0.0 and method == "none":
        suggested = [
            RecommendedRange(
                start_s=0.0,
                end_s=float(duration_s),
                face_ratio=0.0,
                reason="unknown_face_visibility",
            )
        ]

    face_ratio_overall = 0.0
    if samples:
        face_ratio_overall = float(sum(1 for s in samples if s.face_found)) / float(len(samples))

    rep = LipSyncPreviewReport(
        version=1,
        video=str(video),
        duration_s=float(duration_s),
        sample_every_s=float(eff_sample_every_s),
        method=str(method),
        samples=samples,
        face_ratio_overall=float(face_ratio_overall),
        recommended_ranges=recommended,
        suggested_ranges=suggested,
        warnings=warnings,
    )
    logger.info(
        "lipsync_preview_done",
        duration_s=float(duration_s),
        method=str(method),
        samples=int(len(samples)),
        face_ratio_overall=float(face_ratio_overall),
        ranges=int(len(recommended)),
    )
    return rep


def write_preview_report(rep: LipSyncPreviewReport, *, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        out_path, json.dumps(rep.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )
    return out_path
