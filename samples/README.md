## Samples

This repository does **not** commit real media into source control.

If you need a tiny MP4 for local testing:

- Run `./smoke_run.sh` (it generates `samples/Test.mp4` via `ffmpeg` if missing), or
- Generate one manually (offline):

```bash
ffmpeg -y \
  -f lavfi -i "testsrc=size=320x180:rate=10" \
  -f lavfi -i "sine=frequency=440:sample_rate=44100" \
  -t 2.0 \
  -c:v libx264 -pix_fmt yuv420p \
  -c:a aac \
  samples/Test.mp4
```

Most docs use `Input/Test.mp4` as the runtime input path. Copy the sample into `Input/` before running:

```bash
cp samples/Test.mp4 Input/Test.mp4
```

