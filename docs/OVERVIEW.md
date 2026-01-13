## Overview

This repository is an **offline-first dubbing pipeline**. It takes a source video and produces:
- a **dubbed video** (high-quality MKV + browser-friendly MP4)
- subtitle/transcript artifacts (SRT/JSON, optional)
- optional QA reports and a human-in-the-loop review/edit loop

It can be run:
- as a **CLI** (`dubbing-pipeline`) for local processing, or
- as a **FastAPI server + web UI** (`dubbing-web`) for mobile-friendly job submission, progress monitoring, playback, QA, and editing.

---

## Architecture (ASCII)

```text
             (CLI) dubbing-pipeline run                        (Web) dubbing-web
        +---------------------------+           +------------------------------+
        |   reads file from disk    |           |  /ui/* pages + /api/*        |
        |   writes Output/<stem>/   |           |  uploads/jobs/QA/review      |
        +-------------+-------------+           +---------------+--------------+
                      |                                         |
                      v                                         v
               +--------------+                          +--------------+
               | Pipeline  |                          |  Job Queue   |
               | (stages)     |                          | + Scheduler  |
               +------+-------+                          +------+-------+
                      |                                         |
                      +-------------------+---------------------+
                                          v
                                Output/<stem>/... (+ Output/jobs/<job_id>/ pointer)
```

---

## Pipeline stages (high level)

The “happy path” stages look like this. Many steps are **optional** or have **fallbacks** when dependencies are missing.

1) **Extract** audio from the source video
2) **(Optional) Diarize** speakers (who speaks when)
3) **ASR** (speech-to-text) to produce a transcript
4) **(Optional) Translate** transcript to the target language
5) **(Optional) Fit timing / pacing** (reduce overflow, keep speech readable)
6) **TTS** (text-to-speech) to generate dubbed speech audio
7) **(Optional) Separation + enhanced mixing** (ducking/limiter/LUFS targets)
8) **Mux** into outputs (MKV + MP4; optional HLS; optional multi-track)
9) **(Optional) QA scoring** (offline checks + report)

---

## Output folders and key artifacts

### Canonical output directory

By default, outputs go under:
- `Output/<stem>/` where `<stem>` is the input video filename without extension.

When jobs are submitted via the web UI, each job has a stable ID. The job still writes to `Output/<stem>/`, but the server also creates a pointer:
- `Output/jobs/<job_id>/target.txt` → path to the canonical `Output/<stem>/` directory

### Typical structure

```text
Output/<stem>/
  job.log                      # human-readable job log
  .checkpoint.json             # resume-safe stage checkpoint (best-effort)
  <stem>.dub.mkv               # high-quality container (kept)
  <stem>.dub.mp4               # browser-friendly MP4 (kept)
  <stem>.translated.srt        # target subtitles (if enabled)
  <stem>.srt                   # source subtitles (if enabled)
  translated.json              # translated segments (if enabled)
  diarization.json             # diarization output (if enabled)
  audio/
    tracks/                    # per-track artifacts (when enabled)
  mobile/
    mobile.mp4                 # mobile-friendly MP4 for iOS/Android browsers
    hls/                       # optional HLS playlist + segments (if enabled)
  review/
    state.json                 # review/edit state (created by UI or CLI review init)
    overrides.json             # review overrides (when used)
  qa/
    summary.json               # QA score summary (when enabled)
    issues.json                # detailed QA findings (when enabled)
  manifests/                   # per-stage machine-readable metadata (best-effort)
  logs/                        # structured logs (best-effort; server may also write global logs/)
  analysis/                    # debug + retention reports (best-effort)
  work/
    <job_id>/                  # job-specific working directory (temporary/intermediate)
```

Notes:
- Retention and privacy options can delete intermediates after the job finishes (see `docs/FEATURES.md` and `docs/SETUP.md`).
- The exact set of files varies depending on enabled features and which optional dependencies are installed.

---

## Modes (high / medium / low)

Modes control **quality vs speed** and how aggressively the pipeline uses optional features.

- **high**: best quality, slowest (more expensive settings, more work)
- **medium**: balanced default
- **low**: fastest, most degraded (tries to stay usable even with fewer optional deps)

Exact behavior is implemented in the pipeline code and can be verified with:

```bash
python3 scripts/verify_modes_contract.py
```

---

## Where to go next

- Setup: `docs/SETUP.md`
- Full feature list: `docs/FEATURES.md`
- CLI usage: `docs/CLI.md`
- Web/mobile usage: `docs/WEB_MOBILE.md`
- Troubleshooting: `docs/TROUBLESHOOTING.md`

