from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse


# Ensure `src/` is importable when running `uvicorn main:app` from repo root.
REPO_ROOT = Path(__file__).resolve().parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


UPLOADS_DIR = REPO_ROOT / "uploads"
OUTPUTS_DIR = REPO_ROOT / "outputs"


app = FastAPI(title="Anime Dubbing Server")


def _safe_filename(original: str) -> str:
    # Avoid path traversal and weird names; keep a fallback extension if present.
    base = Path(original).name.strip() or "upload"
    return base.replace("\x00", "")


def _save_upload(upload: UploadFile, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("wb") as f:
        shutil.copyfileobj(upload.file, f)


def dub_video(input_path: str, output_path: str) -> None:
    """
    Dubbing hook.

    This project already contains a pipeline entrypoint at `anime_v1.cli:cli`.
    We call it programmatically (similar to `src/anime_v1/ui.py`), then remux
    the resulting MKV into the requested `output_path` (MP4).

    If you have your own function like:
        dub_video(input_path: str, output_path: str)
    you can replace the body of this function with your implementation.
    """

    # 1) Run the existing pipeline, writing its output into OUTPUTS_DIR.
    try:
        from anime_v1.cli import cli as anime_cli  # type: ignore
    except Exception as ex:  # pragma: no cover
        raise RuntimeError(
            "Could not import anime_v1 pipeline. "
            "Make sure dependencies are installed and `src/` is present."
        ) from ex

    in_path = Path(input_path)
    out_dir = Path(output_path).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # CLI emits: <out_dir>/<stem>_dubbed.mkv
    expected_mkv = out_dir / f"{in_path.stem}_dubbed.mkv"

    # Run Click command programmatically.
    # Notes:
    # - default settings are fine; tweak flags here if desired.
    # - standalone_mode=False lets us catch exceptions instead of sys.exit().
    anime_cli.main(
        args=[
            str(in_path),
            "--tgt-lang",
            "en",
            "--out-dir",
            str(out_dir),
        ],
        standalone_mode=False,
    )

    if not expected_mkv.exists():
        raise RuntimeError(f"Pipeline finished but output not found: {expected_mkv}")

    # 2) Remux MKV -> MP4 (fast path: copy streams, no re-encode).
    # If your pipeline already outputs MP4, you can replace this step with a direct write.
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(expected_mkv),
            "-c",
            "copy",
            str(output_path),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    # Simple phone-friendly upload form.
    return """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Anime Dubber</title>
  </head>
  <body>
    <h2>Upload a video to dub</h2>
    <form action="/dub" method="post" enctype="multipart/form-data">
      <input type="file" name="file" accept="video/*" required />
      <button type="submit">Dub</button>
    </form>
  </body>
</html>
""".strip()


@app.post("/dub")
async def dub_endpoint(file: UploadFile | None = File(default=None)) -> FileResponse:
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No file provided. Use multipart form-data with field name 'file'.",
        )

    filename = _safe_filename(file.filename or "upload.mp4")
    upload_id = uuid.uuid4().hex
    upload_path = UPLOADS_DIR / f"{upload_id}_{filename}"
    output_path = OUTPUTS_DIR / "dubbed.mp4"

    try:
        _save_upload(file, upload_path)
    except Exception as ex:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save upload: {ex}",
        ) from ex
    finally:
        try:
            file.file.close()
        except Exception:
            pass

    try:
        dub_video(str(upload_path), str(output_path))
    except HTTPException:
        raise
    except Exception as ex:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Processing failed: {ex}",
        ) from ex

    if not output_path.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Processing finished but output file was not created.",
        )

    return FileResponse(
        path=str(output_path),
        media_type="video/mp4",
        filename="dubbed.mp4",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        reload=False,
    )
