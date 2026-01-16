from __future__ import annotations

import hashlib
import subprocess
from contextlib import suppress
from contextvars import ContextVar
from pathlib import Path

from dubbing_pipeline.config import get_settings
from dubbing_pipeline.utils.io import atomic_write_text, ensure_dir

_FORBIDDEN_FLAGS = {
    "-filter_script",
    "-filter_script:v",
    "-filter_script:a",
    "-stats_file",
}


class FFmpegError(RuntimeError):
    pass


_ffmpeg_log_dir: ContextVar[str | None] = ContextVar("ffmpeg_log_dir", default=None)


def set_ffmpeg_log_dir(path: str | Path | None) -> None:
    _ffmpeg_log_dir.set(str(path) if path else None)


def _write_ffmpeg_logs(argv: list[str], *, stderr: str | None) -> None:
    d = _ffmpeg_log_dir.get()
    if not d:
        return
    out_dir = Path(d)
    ensure_dir(out_dir)
    key = hashlib.sha256((" ".join(argv)).encode("utf-8", errors="replace")).hexdigest()[:16]
    atomic_write_text(out_dir / f"{key}.cmd.txt", " ".join(argv) + "\n")
    if stderr is not None:
        atomic_write_text(out_dir / f"{key}.stderr.log", stderr)


def _validate_args(argv: list[str]) -> None:
    for a in argv:
        if a in _FORBIDDEN_FLAGS:
            raise FFmpegError(f"Forbidden ffmpeg/ffprobe flag: {a}")


def _tail(s: str, n: int = 4000) -> str:
    s = str(s or "")
    if len(s) <= n:
        return s
    return s[-n:]


def run_ffmpeg(
    argv: list[str],
    *,
    timeout_s: int | None = None,
    retries: int = 0,
    capture: bool = False,
) -> subprocess.CompletedProcess[str] | None:
    _validate_args(argv)
    last_ex: Exception | None = None
    for attempt in range(int(retries) + 1):
        try:
            if capture:
                p = subprocess.run(
                    argv,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                with suppress(Exception):
                    _write_ffmpeg_logs(argv, stderr=p.stderr)
                return p
            subprocess.run(
                argv,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
            )
            with suppress(Exception):
                _write_ffmpeg_logs(argv, stderr=None)
            return None
        except subprocess.TimeoutExpired as ex:
            last_ex = ex
            if attempt >= int(retries):
                raise FFmpegError(f"ffmpeg timed out after {timeout_s}s") from ex
        except subprocess.CalledProcessError as ex:
            last_ex = ex
            if attempt >= int(retries):
                stderr = ""
                with suppress(Exception):
                    if ex.stderr:
                        if isinstance(ex.stderr, bytes):
                            stderr = ex.stderr.decode("utf-8", errors="replace")
                        else:
                            stderr = str(ex.stderr)
                with suppress(Exception):
                    _write_ffmpeg_logs(argv, stderr=stderr)
                raise FFmpegError(
                    "ffmpeg failed "
                    f"(exit={ex.returncode})\n"
                    f"argv={argv}\n"
                    f"stderr_tail={_tail(stderr)}"
                ) from ex
        except Exception as ex:
            last_ex = ex
            if attempt >= int(retries):
                with suppress(Exception):
                    _write_ffmpeg_logs(argv, stderr=str(ex))
                raise FFmpegError(f"ffmpeg failed: {ex} (argv={argv})") from ex
    if last_ex:
        raise FFmpegError(f"ffmpeg failed: {last_ex} (argv={argv})") from last_ex
    raise FFmpegError(f"ffmpeg failed (argv={argv})")


def ffprobe_duration_seconds(path: Path, *, timeout_s: int = 20) -> float:
    s = get_settings()
    argv = [
        str(s.ffprobe_bin),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    _validate_args(argv)
    try:
        out = (
            subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=timeout_s)
            .decode("utf-8", errors="replace")
            .strip()
        )
        return float(out)
    except subprocess.TimeoutExpired as ex:
        raise FFmpegError("ffprobe timed out") from ex
    except Exception as ex:
        raise FFmpegError(f"ffprobe failed: {ex}") from ex


def ffprobe_media_info(path: Path, *, timeout_s: int = 20) -> dict:
    """
    Safe ffprobe metadata probe.

    Returns a dict:
      - format_name: str
      - duration_s: float
      - width: int (0 if unknown)
      - height: int (0 if unknown)
    """
    import json

    s = get_settings()
    argv = [
        str(s.ffprobe_bin),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    _validate_args(argv)
    try:
        out = subprocess.check_output(argv, stderr=subprocess.DEVNULL, timeout=timeout_s).decode(
            "utf-8", errors="replace"
        )
    except subprocess.TimeoutExpired as ex:
        raise FFmpegError("ffprobe timed out") from ex
    except Exception as ex:
        raise FFmpegError(f"ffprobe failed: {ex}") from ex

    try:
        data = json.loads(out) if out else {}
    except Exception as ex:
        raise FFmpegError(f"ffprobe returned invalid JSON: {ex}") from ex

    fmt = data.get("format") if isinstance(data, dict) else {}
    streams = data.get("streams") if isinstance(data, dict) else []
    format_name = ""
    duration_s = 0.0
    width = 0
    height = 0

    try:
        if isinstance(fmt, dict):
            format_name = str(fmt.get("format_name") or "").strip()
            duration_s = float(fmt.get("duration") or 0.0)
    except Exception:
        pass

    # Prefer the first video stream.
    if isinstance(streams, list):
        for st in streams:
            if not isinstance(st, dict):
                continue
            if str(st.get("codec_type") or "") != "video":
                continue
            try:
                width = int(st.get("width") or 0)
                height = int(st.get("height") or 0)
            except Exception:
                width = 0
                height = 0
            break

    return {
        "format_name": format_name,
        "duration_s": float(duration_s),
        "width": int(width),
        "height": int(height),
    }


def extract_audio_mono_16k(
    *,
    src: Path,
    dst: Path,
    start_s: float | None = None,
    end_s: float | None = None,
    timeout_s: int = 120,
    retries: int = 0,
) -> None:
    s = get_settings()
    argv = [str(s.ffmpeg_bin), "-y"]
    if start_s is not None:
        argv += ["-ss", f"{float(start_s):.3f}"]
    if end_s is not None:
        argv += ["-to", f"{float(end_s):.3f}"]
    argv += ["-i", str(src), "-ac", "1", "-ar", "16000", str(dst)]
    run_ffmpeg(argv, timeout_s=timeout_s, retries=int(retries))
