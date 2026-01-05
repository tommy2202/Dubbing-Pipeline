## Mode contract matrix (source of truth)

This file defines the **expected behavior** per quality mode. Tests should treat this as the source of truth.

Notes:
- “ON (if available)” means enabled by default in that mode, but may auto-disable when optional deps/hardware are missing, with explicit logging.
- Explicit CLI/API overrides must win over mode defaults.

| Feature | HIGH | MEDIUM | LOW |
|---|---|---|---|
| **ASR model default** | `large-v3` (GPU) else `medium` | `medium` | `small` (or `tiny` if CPU constrained) |
| **Diarization** | ON (if available) | ON (if available) | OFF |
| **Speaker smoothing** | ON | OFF by default (opt-in) | OFF |
| **Voice memory** | ON | OFF by default (opt-in) | OFF |
| **Voice mode default** | `clone` (fallback preset/single) | current default | `single` (fallback preset) |
| **Music/singing detection** | OFF by default (opt-in) | OFF by default (opt-in) | OFF |
| **Separation (Demucs)** | ON (if installed) | OFF | OFF |
| **Mix mode** | `enhanced` (ducking+loudnorm+limiter) | current default | `legacy` / minimal |
| **Timing-fit** | ON | OFF by default (opt-in) | OFF |
| **Pacing** | ON | OFF by default (opt-in) | minimal (pad/trim only) |
| **QA scoring** | ON | OFF by default (opt-in) | OFF |
| **Director mode** | ON | OFF | OFF |
| **Expressive/prosody** | OFF by default (opt-in) | OFF | OFF |
| **Lip-sync plugin** | OFF by default (opt-in; only supported in HIGH) | OFF | OFF |
| **Streaming mode** | OFF by default (opt-in) | OFF by default (opt-in) | OFF by default (opt-in; conservative settings) |
| **Multitrack output** | ON | OFF by default (opt-in) | OFF |

