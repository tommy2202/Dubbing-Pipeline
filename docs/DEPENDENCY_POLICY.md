# Dependency policy (pins + constraints)

## Goals
- Keep installs reproducible on fresh machines without over-pinning.
- Pin only known conflict pairs or binary-compatibility sensitive deps.
- Keep Docker constraints and local constraints aligned.

## Canonical constraints file
- `docker/constraints.txt` is the source of truth for pinned versions.
- Local installs should use:
  - `python3 -m pip install -e . -c docker/constraints.txt`
  - `python3 -m pip install -e ".[web]" -c docker/constraints.txt`

## Current pins (and why)
- `sse-starlette>=2.1.2,<3` in `pyproject.toml`
  - sse-starlette 3.x raises Starlette minimums; we avoid resolver conflicts
    with the FastAPI/Starlette versions in this repo.
  - Docker pins `sse-starlette==2.1.2` for a known-good SSE stack.
- `aiortc==1.9.0`, `av==12.3.0`
  - WebRTC/AV are binary-compat sensitive; pin to known compatible wheels.
  - These are optional and exposed via `.[web]` (and `.[webrtc]`).

## How to update pins safely
1) Update bounds in `pyproject.toml` (only for conflict-prone pairs).
2) Update `docker/constraints.txt` to match the new bounds.
3) Run `python3 scripts/verify_dependency_resolve.py`.
4) If you bump FastAPI/Starlette, re-evaluate sse-starlette compatibility.

## What not to pin
- Do not pin unrelated packages just to quiet the resolver.
- Prefer minimal bounds that address a specific conflict.
