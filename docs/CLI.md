## CLI reference (`anime-v2`)

The CLI is implemented with Click (`src/anime_v2/cli.py`). It has a **default command** (`run`), plus subcommands for review, QA, overrides, voice memory, lip-sync tooling, and character tuning.

### Important quirk: `--help`
Because `run` is the default command, `anime-v2 --help` shows **run** help.

To see other command help:

```bash
anime-v2 run --help
anime-v2 review --help
anime-v2 qa --help
anime-v2 overrides --help
anime-v2 voice --help
anime-v2 lipsync --help
anime-v2 character --help
```

---

## `anime-v2 run` (default)

### What it does
Runs the full pipeline on a local video file (or a batch of files) and writes outputs under `Output/<stem>/`.

### Runtime folders + sample media

- `Input/` and `Output/` are **runtime-only** folders and must not be committed.
- Most examples below assume you have `Input/Test.mp4`. If you don’t, generate a tiny sample and copy it into place:

```bash
ffmpeg -y \
  -f lavfi -i "testsrc=size=320x180:rate=10" \
  -f lavfi -i "sine=frequency=440:sample_rate=44100" \
  -t 2.0 \
  -c:v libx264 -pix_fmt yuv420p \
  -c:a aac \
  samples/Test.mp4

cp samples/Test.mp4 Input/Test.mp4
```

### Basic examples

```bash
# Default (medium, auto device)
anime-v2 Input/Test.mp4

# High quality
anime-v2 Input/Test.mp4 --mode high --device auto

# CPU-only
anime-v2 Input/Test.mp4 --device cpu
```

### Batch processing

```bash
anime-v2 --batch "Input/*.mp4" --jobs 1 --resume
```

Notes:
- `--jobs` exists but batch is currently “best-effort”; the pipeline may still run sequentially.

### Run controls
- `VIDEO` (positional): input file path
- `--batch <dir-or-glob>`: batch input
- `--jobs <N>`: batch worker count (best-effort)
- `--resume/--no-resume` (default: resume)
- `--fail-fast/--no-fail-fast` (default: no-fail-fast)
- `--device auto|cuda|cpu` (default: auto)
- `--mode high|medium|low` (default: medium)

### ASR / translation / subtitles
- `--asr-model <name>` (e.g. `large-v3`)
- `--src-lang <code|auto>` (default: auto)
- `--tgt-lang <code>` (default: en)
- `--no-translate`
- `--mt-provider auto|whisper|marian|nllb|none` (default: auto)
- `--mt-engine auto|whisper|marian|nllb` (default: auto)
- `--mt-lowconf-thresh <float>` (default shown by CLI)
- `--subs off|src|tgt|both` (default: both)
- `--subs-format srt|vtt|both` (default: srt)
- `--no-subs` (do not mux subtitles into the output container)

Examples:

```bash
# Transcribe only (no translation)
anime-v2 Input/Test.mp4 --no-translate --src-lang ja

# Spanish dub
anime-v2 Input/Test.mp4 --src-lang auto --tgt-lang es

# Write both SRT and VTT files
anime-v2 Input/Test.mp4 --subs-format both
```

### Diarization (optional)
- `--diarizer auto|pyannote|speechbrain|heuristic|off` (default: auto)
- `--show-id <id>`: override the persistent character ID namespace
- `--char-sim-thresh <float>` (default shown by CLI)

### Glossary / style / project profiles
- `--glossary <path-or-dir>`
- `--style <path-or-dir>`
- `--project <name>` (looks under `projects/<name>/`)
- `--style-guide <file>` (overrides project)

### Alignment (optional)
- `--aligner auto|aeneas|heuristic` (default: auto)
- `--align-mode basic|stretch|word` (default: stretch)
- `--max-stretch <float>` (default shown by CLI)

### Mixing / separation (optional)
- `--mix legacy|enhanced` (default: legacy)
- `--mix-profile streaming|broadcast|simple` (default: streaming)
- `--separate-vocals/--no-separate-vocals` (default shown by CLI)
- `--separation off|demucs` (default: off)
- `--separation-model <name>` (default: htdemucs)
- `--separation-device auto|cpu|cuda` (default: auto)
- `--lufs-target <float>` (default: -16)
- `--ducking/--no-ducking` (default: ducking)
- `--ducking-strength <float>` (default: 1.0)
- `--limiter/--no-limiter` (default: limiter)

Example:

```bash
anime-v2 Input/Test.mp4 --mix enhanced --separation demucs
```

### Timing fit + pacing (optional)
- `--timing-fit/--no-timing-fit` (default: off)
- `--pacing/--no-pacing` (default: off)
- `--pacing-min-stretch <float>` (default: 0.88)
- `--pacing-max-stretch <float>` (default: 1.18)
- `--wps <float>` (default: 2.7)
- `--tolerance <float>` (default: 0.1)
- `--timing-debug`
- `--rewrite-provider heuristic|local_llm` (default: heuristic)
- `--rewrite-endpoint http://127.0.0.1:...` (optional; local-only)
- `--rewrite-model <path>` (optional)
- `--rewrite-strict/--no-rewrite-strict` (default: rewrite-strict)

### Streaming / “realtime” chunk mode (optional)
- `--realtime/--no-realtime` (default: off)
- `--stream off|on` (alias for realtime; default off)
- `--chunk-seconds <float>` (default: 20)
- `--chunk-overlap <float>` (default: 2)
- `--overlap-seconds <float>` (alias for chunk-overlap)
- `--stream-context-seconds <float>` (optional)
- `--stream-output segments|final` (default: segments)
- `--stream-concurrency <int>` (default: 1)
- `--stitch/--no-stitch` (default: stitch)

### Music/singing detection + OP/ED detection (optional)
- `--music-detect off|on` (default: off)
- `--music-mode auto|heuristic|classifier` (default: auto)
- `--music-threshold <float>` (default: 0.7)
- `--op-ed-detect off|on` (default: off)
- `--op-ed-seconds <int>` (default: 90)

### Speaker smoothing / scene detection (optional)
- `--speaker-smoothing off|on` (default: off)
- `--scene-detect off|audio` (default: audio)
- `--smoothing-min-turn <float>` (default: 0.6)
- `--smoothing-surround-gap <float>` (default: 0.4)

### PG mode (optional)
- `--pg off|pg13|pg` (default: off)
- `--pg-policy <file>` (optional JSON overrides)

### QA scoring (optional)
- `--qa off|on` (default: off)

### “Dub Director” + expressive prosody (optional)
- `--director off|on` (default: off)
- `--director-strength <float>` (default: 0.5)
- `--emotion-mode off|auto|tags` (default: off)
- `--expressive off|auto|source-audio|text-only` (default: off)
- `--expressive-strength <float>` (default: 0.5)
- `--expressive-debug`
- `--speech-rate <float>` (default: 1.0)
- `--pitch <float>` (default: 1.0)
- `--energy <float>` (default: 1.0)

### Output formats
- `--emit <comma-list>`: always includes `mkv,mp4` and may include:
  - `fmp4` (fragmented MP4)
  - `hls` (HLS export)

### Multi-track outputs (optional)
- `--multitrack off|on` (default: off)
- `--container mkv|mp4` (default: mkv)

### Voice controls
- `--voice-mode clone|preset|single` (default: clone)
- `--voice-ref-dir <path>`
- `--voice-store <path>`
- `--voice-memory off|on` (default: off)
- `--voice-memory-dir <path>`
- `--voice-match-threshold <float>` (default shown by CLI)
- `--voice-auto-enroll/--no-voice-auto-enroll` (default: on)
- `--voice-character-map <path>`
- `--list-characters`
- `--rename-character <id> <name>` (repeatable)
- `--set-character-voice-mode <id> clone|preset|single` (repeatable)
- `--set-character-preset <id> <preset_voice_id>` (repeatable)
- `--tts-provider auto|xtts|basic|espeak` (default: auto)

### Lip-sync plugin (optional)
- `--lipsync off|wav2lip` (default: off)
- `--wav2lip-dir <path>`
- `--wav2lip-checkpoint <path>`
- `--lipsync-face auto|center|bbox`
- `--lipsync-device auto|cpu|cuda`
- `--lipsync-box "x1,y1,x2,y2"`
- `--lipsync-scene-limited off|on` (default: off)
- `--lipsync-sample-every <float>` (default: 0.5)
- `--lipsync-min-face-ratio <float>` (default: 0.6)
- `--lipsync-min-range <float>` (default: 2.0)
- `--lipsync-merge-gap <float>` (default: 0.6)
- `--lipsync-max-frames <int>` (default: 600)
- `--strict-plugins` (fail if requested plugin is missing)

### Logging / debugging
- `--print-config` (safe config report; no secrets)
- `--dry-run` (validate tools/inputs and exit)
- `--verbose` (INFO)
- `--debug` (DEBUG)
- `--log-level critical|error|warning|info|debug` (default: INFO)
- `--log-json off|on` (default: on)
- `--debug-dump` (extra artifacts under `Output/<stem>/analysis/`)

### Retention + privacy (optional; default off)
- `--cache-policy full|balanced|minimal` (default: full)
- `--retention-days <int>` (default: 0)
- `--retention-dry-run`
- `--privacy off|on` (default: off)
- `--no-store-transcript`
- `--no-store-source-audio`
- `--minimal-artifacts`

---

## `anime-v2 review ...` (review/edit loop)

```bash
anime-v2 review init <input_video>
anime-v2 review list <job>
anime-v2 review show <job> <segment_id>
anime-v2 review edit <job> <segment_id> --text "New line"
anime-v2 review regen <job> <segment_id>
anime-v2 review play <job> <segment_id>
anime-v2 review lock <job> <segment_id>
anime-v2 review unlock <job> <segment_id>
anime-v2 review render <job>
```

`<job>` may be:
- a job directory path, or
- a job name under `Output/` (same behavior as QA commands)

---

## `anime-v2 qa ...` (quality scoring)

```bash
anime-v2 qa run <job> --top 20
anime-v2 qa run <job> --fail-only
anime-v2 qa show <job>
```

---

## `anime-v2 overrides ...` (music/speaker overrides)

Apply effective artifacts after edits:

```bash
anime-v2 overrides apply <job>
```

Music regions:

```bash
anime-v2 overrides music list <job>
anime-v2 overrides music add <job> --start 10.0 --end 20.0 --kind music --reason "OP"
anime-v2 overrides music edit <job> --from-start 10.0 --from-end 20.0 --start 9.5 --end 20.5
anime-v2 overrides music remove <job> --start 9.5 --end 20.5
```

Speaker overrides:

```bash
anime-v2 overrides speaker set <job> <segment_id> <character_id>
anime-v2 overrides speaker unset <job> <segment_id>
```

---

## `anime-v2 voice ...` (voice memory tools)

```bash
anime-v2 voice list
anime-v2 voice audition --text "Hello there" --lang en --top 3
anime-v2 voice merge <from_id> <to_id> [--move-refs] [--keep-alias]
anime-v2 voice undo-merge <merge_id>
```

---

## `anime-v2 lipsync preview ...` (helper)

```bash
anime-v2 lipsync preview Input/Test.mp4 --out-dir Output/Test/lipsync_preview
```

---

## `anime-v2 character ...` (per-character tuning)

```bash
anime-v2 character set-voice-mode <character_id> clone|preset|single
anime-v2 character set-rate <character_id> 1.05
anime-v2 character set-style <character_id> normal
anime-v2 character set-expressive <character_id> 0.6
```

---

## Common recipes

### Fast “good enough” CPU run
```bash
anime-v2 Input/Test.mp4 --mode low --device cpu --no-translate
```

### High quality with QA
```bash
anime-v2 Input/Test.mp4 --mode high --qa on
```

### Mobile-friendly HLS export (optional)
```bash
anime-v2 Input/Test.mp4 --emit mkv,mp4,hls
```

### Minimal retention (privacy-friendly)
```bash
anime-v2 Input/Test.mp4 --privacy on --cache-policy minimal
```

