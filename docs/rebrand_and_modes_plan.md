## Rebrand + Quality Modes plan

### Terms to replace (targets)

This rebrand is **user-facing**. We will not rename Python packages (`anime_v2`, `anime_v1`) to avoid breaking imports.

- **User-facing strings**
  - “anime dubbing”, “Anime dubbing”, “Anime v2”, “anime_v2”, “anime-v2”, “anime-v1”
  - “v1”, “v2”, “alpha” (when used as product/version branding)
  - log prefixes like `"[v2]"`, UI page titles like “Upload • anime_v2”
- **Internal identifiers**
  - Keep `anime_v2` / `anime_v1` module names as-is (stability).
  - Keep existing output filenames unless they embed version branding (they mostly don’t).

### File paths likely affected

- **CLI / entrypoints**
  - `pyproject.toml` (`[project.scripts]`: add `dub` / `dubbing-web` aliases; keep existing names for compatibility)
  - `src/anime_v2/cli.py` (help strings, log messages, default group name)
  - `src/anime_v1/cli.py` (help strings)
- **Web UI / API**
  - `src/anime_v2/server.py`, `src/anime_v2/web/app.py` (FastAPI titles)
  - `src/anime_v2/web/templates/*.html` (page titles, navbar, inline help)
  - `src/anime_v2/web/routes_jobs.py` (returned labels/messages if any)
- **Docs**
  - `README.md` (main branding; keep any historical “v1/v2” only in changelog/history sections)
  - `docs/*.md` (same rule; history sections may mention versions)
- **Logging**
  - Repo-wide: any `"[v2]"` or “anime_v2” branding in log messages
  - Per-job logs already exist (`Output/<job>/logs/*`) and should remain but with neutral wording.

### Renaming strategy (safe incremental)

1) **Introduce user-facing command aliases first**
   - Add `dub` (alias of `anime_v2.cli:cli`) and `dubbing-web` (alias of `anime_v2.web.run:main`).
   - Keep `anime-v2` and `anime-v2-web` for backward compatibility.
2) **Rebrand UI titles and navbar text**
   - Replace “anime_v2” with “Dubbing” (or “Dubbing Server”) in templates and FastAPI titles.
3) **Rebrand CLI help text and examples**
   - Update Click help strings to reference `dub` (but keep `anime-v2` working).
4) **Remove version branding from logs**
   - Replace `"[v2]"` prefixes with neutral stage tags (e.g. `audio_extractor`, `transcribe`, `mix`, `mux`).
5) **Outputs**
   - Keep current output structure (`Output/<job>/...`) and filenames unless they embed version branding.
   - If any user-facing filenames include “v1/v2/alpha”, either:
     - write both filenames for one release cycle, or
     - write the new name and keep a compatibility copy.

### Quality modes: contract + enforcement plan

#### Goals

- **HIGH**: best quality, enable advanced features **when available**, with explicit fallback logging.
- **MEDIUM**: current default behavior (must not change defaults), balanced quality.
- **LOW**: minimal CPU-friendly pipeline (no expensive optional stages by default).

#### Mode resolver

Add a single resolver module (no package rename conflicts):

- Proposed location: `src/anime_v2/modes.py`
  - `Mode = Literal["high","medium","low"]`
  - `ModeProfile` (defaults for each mode)
  - `HardwareCaps` detector (GPU availability; optional deps availability)
  - `resolve_effective(mode_requested, cli_args, settings) -> EffectiveConfig`
    - Mode defaults apply **only when the user did not explicitly override**.
    - Hardware fallback applies by **adjusting only the incompatible parts** and logging reasons.

#### Feature matrix (contract)

| Feature / stage | HIGH | MEDIUM (default today) | LOW |
|---|---:|---:|---:|
| **ASR model** | `large-v3` if GPU else `medium` | `medium` (current) | `small`/`tiny` CPU |
| **Device selection** | `auto` (prefer GPU) | `auto` | force CPU-friendly defaults |
| **Diarization** | on if available | on if available (current behavior) | off |
| **Speaker smoothing** | on | optional/off by default | off |
| **Voice memory** | on | optional/off by default | off |
| **Voice mode** | clone (if deps) else preset | current default | single/preset (fast) |
| **Music/singing detection** | optional/on if enabled by mode policy | optional/off by default | off |
| **Separation (Demucs)** | on if installed | off | off |
| **Enhanced mixing** | on (loudnorm+ducking+limiter) | current default | minimal/legacy |
| **Timing-fit** | on | optional/off by default | off |
| **Pacing** | on | optional/off by default | minimal (pad/trim only) |
| **QA scoring** | on | optional/off by default | off |
| **Review loop** | available | available | available |
| **Director mode** | on | off | off |
| **Expressive (prosody)** | optional | off | off |
| **Lip-sync plugin** | optional (only in HIGH; still off unless requested) | off | off |
| **Streaming mode** | optional | optional | optional (conservative settings) |
| **Multitrack output** | on | optional | off |

Notes:
- “on if available” means the resolver checks optional dependency presence (and GPU capability where relevant). If missing, it disables that feature **and logs why**.
- MEDIUM must remain the default path to preserve behavior. HIGH/LOW may adjust defaults but will honor explicit CLI overrides.

### Web UI wiring plan (modes)

- UI label: change “Mode” → **“Quality”** (values `high|medium|low` unchanged for compatibility).
- API/job runtime:
  - Store `requested_mode` and `effective_mode` + a `mode_summary` dict under `job.runtime`.
  - Expose it on job detail page.

