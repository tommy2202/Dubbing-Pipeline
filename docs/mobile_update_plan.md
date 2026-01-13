# Mobile Update Plan (STOP after this doc)

This plan is based on a repo-wide scan of the current FastAPI server, UI templates, auth/security code, and deploy manifests.

## Current web/mobile feature map (what exists today)

### Canonical server entrypoint (production-grade)
- **App**: `src/dubbing_pipeline/server.py`
  - **Routers mounted**
    - Auth: `src/dubbing_pipeline/api/routes_auth.py` mounted at `/auth/*` **and** `/api/auth/*`
    - API keys: `src/dubbing_pipeline/api/routes_keys.py` (`/keys/*`)
    - Settings: `src/dubbing_pipeline/api/routes_settings.py` (`/api/settings/*`, `/api/admin/*`)
    - Audit: `src/dubbing_pipeline/api/routes_audit.py` (`/api/audit/recent`)
    - Runtime: `src/dubbing_pipeline/api/routes_runtime.py` (`/api/runtime/state`)
    - Jobs/UI/API surface: `src/dubbing_pipeline/web/routes_jobs.py` (many `/api/*` routes + events + WS)
    - UI pages: `src/dubbing_pipeline/web/routes_ui.py` (`/ui/*`)
    - WebRTC preview: `src/dubbing_pipeline/web/routes_webrtc.py` (`/webrtc/*`)
  - **Static/templates**
    - Templates: `src/dubbing_pipeline/web/templates/*`
    - Static: `src/dubbing_pipeline/web/static/*` (mounted at `/static`)
  - **Playback/file serving**
    - `GET /video/{job}`: Range streaming for a resolved job output.
    - `GET /files/{path:path}`: Range streaming for artifacts under `Output/` (includes HLS `.m3u8` / `.ts`).
  - **Security middleware**
    - CORS is configured via `get_settings().cors_origin_list()` (credentials enabled).
    - Request context + per-request logging: `dubbing_pipeline.api.middleware.request_context_middleware` + `http_done` logging.
  - **Queue**
    - In-proc queue + scheduler are started in lifespan, wired to `JobStore` / `JobQueue`.

### UI pages (mobile-relevant)
- **Login**: `GET /ui/login` → `web/templates/login.html`
  - Uses `/api/auth/login` and requests a signed session cookie (recommended for UI).
  - CSRF cookie + hidden input are used for browser state-changing calls.
- **Dashboard (job cards/table)**: `GET /ui/dashboard` → `web/templates/dashboard.html` + `web/templates/_jobs_table.html`
  - Poll-based refresh + links to job detail.
- **Upload wizard (job submission)**: `GET /ui/upload` → `web/templates/upload_wizard.html`
  - Multi-step, supports **upload file(s)** or **server-file selection** (under `APP_ROOT`).
  - Includes “PG Mode (session-only)” and “Quality checks” toggles.
- **Job detail (mobile playback + QA→Fix)**: `GET /ui/jobs/{job_id}` → `web/templates/job_detail.html`
  - Artifacts tab includes HLS-first playback, MP4 fallback, download links, and a **QR code** (`GET /api/jobs/{id}/qrcode`).
  - Includes “Quality” tab and “Transcript” tab which link into review/segment tooling.
  - Includes “sticky mobile player controls” footer (touch-friendly).

### Jobs + uploads + progress APIs (mobile-critical)
All implemented in `src/dubbing_pipeline/web/routes_jobs.py`:

- **Job submission**
  - `POST /api/jobs`:
    - Accepts `multipart/form-data` with `file` upload (saved under `APP_ROOT/Input/uploads/`) OR `video_path` referencing a server file under `APP_ROOT`.
    - Also accepts `application/json` requiring `video_path`.
    - Has:
      - **Idempotency-Key support**
      - **Per-identity rate limiting**
      - **Disk free-space guard**
      - **Duration validation** (ffprobe)
      - **Per-job session flags** stored in `job.runtime` (PG, QA, project/profile, cache policy, smoothing/director toggles).
  - `POST /api/jobs/batch` for multi-file submissions.
- **Job state + listing**
  - `GET /api/jobs` (list)
  - `GET /api/jobs/{id}` (detail)
  - `POST /api/jobs/{id}/cancel|pause|resume`
- **Progress streaming**
  - `GET /api/jobs/events` (SSE)
  - `GET /events/jobs/{id}` (SSE)
  - `WS /ws/jobs/{id}` (WebSocket)
- **Logs**
  - `GET /api/jobs/{id}/logs/tail`
  - `GET /api/jobs/{id}/logs/stream` (SSE)
- **Playback discovery**
  - `GET /api/jobs/{id}/files` returns URLs for:
    - HLS manifest (if present)
    - MP4 (if present)
    - MKV (if present)
    - Lipsync MP4 (if present)
    - Subtitle variants, multitrack artifacts, retention report, etc.
- **Review loop integration**
  - `GET /api/jobs/{id}/review/segments`
  - `POST /api/jobs/{id}/review/segments/{segment_id}/edit|regen|lock|unlock`
  - `GET /api/jobs/{id}/review/segments/{segment_id}/audio` (Range)
- **Overrides integration**
  - `GET|PUT /api/jobs/{id}/overrides`
  - `POST /api/jobs/{id}/overrides/apply`
  - `POST /api/jobs/{id}/overrides/speaker` (segment-level speaker/character override)

### Auth/security model (what exists today)
- **JWT + Refresh cookie + optional session cookie**
  - `src/dubbing_pipeline/api/routes_auth.py`: `/auth/login`, `/auth/refresh`, `/auth/logout`, TOTP endpoints.
  - `src/dubbing_pipeline/api/security.py`: issues/decodes JWT, CSRF tokens.
  - `src/dubbing_pipeline/api/deps.py`: `current_identity()` supports:
    - API keys (`dp_<prefix>_<secret>`) via `X-Api-Key` or `Authorization: Bearer dp_...`
    - Bearer access token via `Authorization: Bearer ...`
    - **Bearer token via query param `?token=...`** (explicit comment: for `<video>` tags)
    - Signed `session` cookie (web UI mode)
- **CSRF**
  - Double-submit cookie header token validation in `src/dubbing_pipeline/api/security.py`.
  - Enforcement currently occurs in:
    - `require_role/require_scope` (good: state-changing only)
    - **AND also inside `current_identity()` for `session` cookies** (problematic; see conflicts/risks below)
- **Token redaction**
  - `src/dubbing_pipeline/utils/log.py` redacts JWT/Bearer-looking strings and common secret keys in log messages.

### Playback outputs (mobile compatibility today)
- Emission already supports:
  - **MP4** (`dub.mp4`)
  - **Fragmented MP4** (`dub.frag.mp4`) when enabled
  - **HLS** (`Output/<job>/.../master.m3u8`) when enabled
  - **MKV** (`dub.mkv`) as the richest container (multi-track support)
  - Player logic in `job_detail.html` prefers HLS, falls back to MP4, else download links.

### Remote access story (what exists today)
- **Cloudflare Tunnel** (outbound-only, no inbound ports):
  - `deploy/compose.tunnel.yml` runs `cloudflared tunnel run --token ${CLOUDFLARE_TUNNEL_TOKEN}`.
- **Public TLS reverse-proxy via Caddy** (requires inbound 80/443):
  - `deploy/compose.public.yml` runs Caddy on 80/443 with an API container behind it.

## Conflict list (obsolete frameworks/stubs) + delete vs reroute

### Conflict 1: Legacy web app with permissive CORS + token-in-URL flow
- **File**: `src/dubbing_pipeline/web/app.py`
  - Uses `allow_origins=["*"]` and `verify_api_key()` with a query-token `/login` cookie setter.
  - Implements its own Range streaming for `/video/{job}`.
  - Serves `web/templates/index.html` which stores an access token in `localStorage`.
- **Why it conflicts**
  - Duplicates capabilities already implemented more securely in `src/dubbing_pipeline/server.py`.
  - Encourages token-in-URL + localStorage patterns that are explicitly disallowed by the Mobile Update goals.
- **Plan**
  - **Delete** `src/dubbing_pipeline/web/app.py` and `src/dubbing_pipeline/web/run.py` *or* reroute them into a thin compatibility shim that imports `dubbing_pipeline.server:app` and does not define a second FastAPI app.
  - If any docs/scripts still reference `dubbing_pipeline.web.run`, update them to use `dubbing_pipeline.server`.

### Conflict 2: CSRF enforcement blocks cookie-authenticated media playback
- **File**: `src/dubbing_pipeline/api/deps.py` (`current_identity()` session-cookie branch)
  - Enforces `verify_csrf(request)` even for **GET** requests when `session` cookie is used.
- **Why it conflicts**
  - Media elements (`<video>`, HLS segment fetches) cannot attach `X-CSRF-Token` headers.
  - This pushes the system to use `?token=...` in the URL for playback, which is a known leak vector.
- **Plan**
  - **Reroute** CSRF enforcement so that:
    - Cookie sessions are accepted for **GET/HEAD** without CSRF.
    - CSRF is enforced only for **state-changing requests**, via `require_role/require_scope` (already present).
  - This enables secure, headerless playback on mobile while maintaining CSRF protection for mutations.

### Conflict 3: External CDN dependencies break “offline-first” and can be flaky on mobile
- **Files**:
  - `src/dubbing_pipeline/web/templates/job_detail.html` loads `video.js` + `hls.js` from CDNs.
- **Why it conflicts**
  - On mobile networks with captive portals / restricted DNS, CDNs can fail even when the tunnel works.
  - “Offline-first” should avoid hard runtime dependencies on third-party CDNs.
- **Plan**
  - **Reroute** to locally vendored JS/CSS under `src/dubbing_pipeline/web/static/vendor/` and load from `/static/...`.

### Conflict 4: Token-in-URL support is explicitly enabled
- **File**: `src/dubbing_pipeline/api/deps.py` (query param `?token=` support)
- **Plan**
  - Keep temporarily for backwards compatibility, but:
    - Introduce a “signed ephemeral media link” mechanism as a safer replacement for embedding credentials in URLs.
    - Provide a migration path and eventually disable `?token=` for non-development contexts.

## Proposed canonical module structure (Mobile Update target)

Goal: one clean, auditable implementation for mobile access, auth, uploads, playback, and UI—without parallel “web apps”.

### Canonical FastAPI app
- **Keep** `src/dubbing_pipeline/server.py` as the only app entrypoint.
- **Remove/replace** `src/dubbing_pipeline/web/app.py` and `src/dubbing_pipeline/web/run.py`.

### Auth (browser + API)
- **Auth routes**: keep `src/dubbing_pipeline/api/routes_auth.py`
- **Auth deps**: refactor within `src/dubbing_pipeline/api/deps.py` to:
  - support cookie sessions for GET media
  - remove/limit `?token=` usage
  - ensure no secret/token values reach logs
- **New module (proposed)**: `src/dubbing_pipeline/api/media_tokens.py`
  - Issues short-lived, scope-limited tokens for `/files/*` playback (optional; see staged plan).

### Uploads (resumable/chunked + server-file selection)
- **Existing**: `POST /api/jobs` supports simple uploads and server file selection.
- **New modules (proposed)**
  - `src/dubbing_pipeline/web/uploads.py`:
    - upload sessions, chunk verification, finalization
    - anti-abuse bounds (max size, timeouts, per-identity limits)
  - `src/dubbing_pipeline/web/routes_uploads.py`:
    - `POST /api/uploads/init`
    - `PATCH /api/uploads/{upload_id}` (Content-Range / chunked)
    - `POST /api/uploads/{upload_id}/complete`
    - `DELETE /api/uploads/{upload_id}`
  - Integrate with `POST /api/jobs` to accept `upload_id` as an alternative to `file`/`video_path`.

### Jobs/queue + progress (mobile-friendly)
- **Keep**: `src/dubbing_pipeline/web/routes_jobs.py` as canonical job API surface.
- **Add**: a “mobile-first status endpoint” for polling (low overhead) (proposed)
  - `GET /api/jobs/{id}/status` → minimal JSON (state, progress, message, updated_at, urls-ready)
- **Keep** SSE + WS for live progress; for mobile data, polling fallback is important.

### Player + mobile outputs (MP4/HLS compatibility)
- **Keep**: `/files/*` Range support in `src/dubbing_pipeline/server.py`.
- **Add** (proposed):
  - `src/dubbing_pipeline/web/player.py`:
    - choose best playback source for device (HLS preferred; MP4 fallback; download)
    - prepare “preview-friendly” MP4 profile settings when needed (faststart, baseline, AAC) as an optional export mode.
- **UI integration**:
  - `job_detail.html` already prefers HLS, falls back to MP4.
  - Replace CDN assets with local vendor assets.

### QA + review UI integration (QA → Fix workflow)
- **Keep**: `/api/jobs/{id}/review/*` and transcript endpoints in `routes_jobs.py`.
- **UI plan**
  - Ensure “Fix” actions from the QA tab deep-link to the specific segment editor state (review loop) using existing endpoints.
  - Add a simplified “Mobile view” on job detail:
    - big “Play”
    - “Top issues” list
    - “Fix next” button (walks through failing segments sequentially)

## Remote access plan (mobile data, no inbound ports)

### Primary: Tailscale (private, app-based)
- **Rationale**
  - Works over cellular without opening inbound ports.
  - Strong device-auth and private addressing; no public exposure by default.
- **Deliverables (proposed)**
  - `deploy/tailscale/compose.tailscale.yml`:
    - runs `tailscaled` (or `tailscale serve`) sidecar, exposes `https://<device>.tailnet-xxx.ts.net` to the API container.
  - `deploy/tailscale/setup.sh`:
    - brings up tailscale, enables serve/proxy, prints the stable URL.
  - `docs/mobile_remote_access_tailscale.md`:
    - phone setup steps + common troubleshooting + security notes.

### Optional fallback: Cloudflare Tunnel + Access (browser-based)
- **Rationale**
  - No app required on the phone; works in browser.
  - Cloudflare Access can add an additional auth wall in front of the app.
- **What already exists**
  - `deploy/compose.tunnel.yml` (cloudflared outbound-only tunnel)
- **Deliverables (proposed)**
  - `deploy/cloudflare/README.md`:
    - Access policy recommendations (SSO, OTP)
    - `Referrer-Policy` + cookie secure settings guidance
  - Optional “Access headers” integration if needed (document-only unless required).

## Staged implementation checklist (with file paths + tests)

### Stage 0 — Safety baseline & deconfliction
- **Delete/reroute legacy app**
  - Remove `src/dubbing_pipeline/web/app.py` and `src/dubbing_pipeline/web/run.py` or make them import `dubbing_pipeline.server:app`.
- **Verification**
  - New script: `scripts/verify_mobile_no_legacy_server.py`
    - asserts only `dubbing_pipeline.server:app` is used by docker/entrypoints

### Stage 1 — Fix cookie-auth playback without token-in-URL
- **Change**
  - Refactor `src/dubbing_pipeline/api/deps.py` so `session` cookie auth does **not** require CSRF on GET/HEAD.
  - Keep CSRF enforcement for state-changing requests via `require_role/require_scope`.
- **Verification**
  - `scripts/verify_mobile_playback_cookie_auth.py`:
    - logs in, gets cookies, fetches `/files/...` without `X-CSRF-Token` header, expects 206 and `Accept-Ranges`.

### Stage 2 — Replace token-in-URL with safer media access (optional but recommended)
- **Change**
  - Add `src/dubbing_pipeline/api/media_tokens.py` and endpoints:
    - `POST /api/jobs/{id}/media-link` → short-lived signed URL for a specific artifact (path + expiry + scope)
  - Add headers on HTML responses:
    - `Referrer-Policy: no-referrer`
    - `X-Content-Type-Options: nosniff`
    - `Content-Security-Policy` (tight, template-compatible)
- **Verification**
  - `scripts/verify_mobile_media_links.py`

### Stage 3 — Resumable/chunked upload support
- **Change**
  - Add upload session endpoints (init/chunk/complete) + storage under `APP_ROOT/Input/uploads/`.
  - Wire `POST /api/jobs` to accept `upload_id`.
- **Verification**
  - `scripts/verify_mobile_resumable_upload.py`:
    - uploads a file in chunks, completes, submits a job, polls status.

### Stage 4 — Mobile playback reliability (codec/container fallbacks)
- **Change**
  - Ensure exports include at least one “mobile-safe” option:
    - HLS (preferred for iOS)
    - MP4 with faststart + AAC (fallback)
  - Vendor `video.js` / `hls.js` locally under `src/dubbing_pipeline/web/static/vendor/`.
- **Verification**
  - `scripts/verify_mobile_outputs_present.py`:
    - creates a dummy job output folder (synthetic) and validates `/api/jobs/{id}/files` returns a playable source preference order.

### Stage 5 — UX improvements for mobile workflow (simple/advanced + QA→Fix)
- **Change**
  - `web/templates/upload_wizard.html`: explicit “Simple” vs “Advanced” sections (persisted per user settings, not global).
  - `web/templates/job_detail.html`: “Fix next issue” loop and clear “Open editor for segment X”.
- **Verification**
  - `scripts/verify_mobile_ui_routes.py` (smoke: pages render + require auth).

### Stage 6 — Hardening for remote exposure behind tunnel
- **Change**
  - Add security headers middleware in `src/dubbing_pipeline/api/middleware.py` (or new module):
    - HSTS when `COOKIE_SECURE=1`
    - CSP (no inline scripts where feasible; otherwise strict nonces)
  - Tighten rate limits for auth + uploads + job submission.
  - Ensure audit logs cover upload creation/completion and job submissions.
- **Verification**
  - `scripts/verify_mobile_security_headers.py`
  - `scripts/verify_mobile_rate_limits.py`

## “Done” criteria for Mobile Update (mapped to your goals)
- Remote access over mobile data: **Tailscale primary + Cloudflare Tunnel optional** documented and scripted.
- Secure auth: **no token in URL required** for playback; no token leaks in logs; cookies + CSRF done correctly.
- Reliable mobile playback: **HLS-first, MP4 fallback**, local JS assets (no CDN dependency).
- Job submission works: **upload → queue → progress → results** verified by scripts.
- Resumable/chunked uploads: implemented with clear limits and verification.
- Mobile-friendly UX: upload wizard “simple/advanced”, job cards, QA→Fix loop.
- Security hardening: TLS behind tunnel, CORS/CSRF correct, rate limits, audit logs.
- No code conflicts: legacy web stubs removed/rerouted, single canonical server.
- Detailed logging + verification scripts: added and runnable without real media.

