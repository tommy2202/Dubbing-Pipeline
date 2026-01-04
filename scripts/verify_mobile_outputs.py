from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from anime_v2.server import _range_stream  # type: ignore
from anime_v2.stages.export import export_mobile_hls, export_mobile_mp4


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, check=True)  # nosec B603
    return p.stdout.strip()


def _ffprobe_json(path: Path) -> dict:
    out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ]
    )
    return json.loads(out)


def _make_dummy_inputs(tmp: Path) -> tuple[Path, Path]:
    video = tmp / "in.mp4"
    wav = tmp / "dub.wav"
    subprocess.run(  # nosec B603
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "2.0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(  # nosec B603
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:sample_rate=44100",
            "-t",
            "2.0",
            str(wav),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return video, wav


def _assert_codecs(mp4: Path) -> None:
    info = _ffprobe_json(mp4)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    assert v and v.get("codec_name") == "h264", f"video codec not h264: {v}"
    assert a and a.get("codec_name") == "aac", f"audio codec not aac: {a}"


def _range_app(path: Path) -> FastAPI:
    app = FastAPI()

    @app.get("/file")
    async def file(request: Request):
        gen, start, end, size = _range_stream(path, request.headers.get("range"))
        headers = {
            "Accept-Ranges": "bytes",
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
        }
        return StreamingResponse(gen, status_code=206, media_type="video/mp4", headers=headers)

    return app


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        video, dub_wav = _make_dummy_inputs(tmp)

        mobile_mp4 = tmp / "mobile.mp4"
        orig_mp4 = tmp / "original.mp4"
        export_mobile_mp4(video_in=video, audio_wav=dub_wav, out_path=mobile_mp4)
        export_mobile_mp4(video_in=video, audio_wav=None, out_path=orig_mp4)
        assert mobile_mp4.exists() and mobile_mp4.stat().st_size > 0
        assert orig_mp4.exists() and orig_mp4.stat().st_size > 0
        _assert_codecs(mobile_mp4)
        _assert_codecs(orig_mp4)

        # Optional HLS export smoke
        hls_dir = tmp / "hls"
        master = export_mobile_hls(video_in=video, dub_wav=dub_wav, out_dir=hls_dir)
        assert master.exists()
        idx = hls_dir / "index.m3u8"
        assert idx.exists()
        # at least one ts segment
        segs = list(hls_dir.glob("seg_*.ts"))
        assert segs, "no HLS segments produced"

        # Range header test (206 + Accept-Ranges + Content-Range)
        app = _range_app(mobile_mp4)
        with TestClient(app) as c:
            r = c.get("/file", headers={"Range": "bytes=0-99"})
            assert r.status_code == 206
            assert r.headers.get("accept-ranges") == "bytes"
            assert (r.headers.get("content-range") or "").startswith("bytes ")
            assert int(r.headers.get("content-length") or "0") == len(r.content)

        print("verify_mobile_outputs: OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

