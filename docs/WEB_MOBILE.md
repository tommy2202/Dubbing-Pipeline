## Web + Mobile guide

This guide covers the **FastAPI server + web UI** (`dubbing-web`) and how to use it from a phone (LAN or remote).

If you only want remote setup details, also see:
- `docs/mobile_remote.md`
- `docs/remote_access.md`

---

## Start the web server

### Local/LAN (default)

```bash
export REMOTE_ACCESS_MODE=off
export HOST=0.0.0.0
export PORT=8000
dubbing-web
```

Health:
- `http://<SERVER_IP>:8000/healthz`

UI:
- `http://<SERVER_IP>:8000/ui/login`

---

## Login (safe method)

### Recommended: cookie session (for browsers)
The login page (`/ui/login`) signs you in using a **signed session cookie** and sets a CSRF cookie.

Security notes:
- Cookies are `HttpOnly` where appropriate.
- State-changing requests require CSRF (`csrf` cookie + `X-CSRF-Token` header).
- For real remote use over HTTPS, set `COOKIE_SECURE=1`.

### API clients (non-browser)
You can also authenticate with:
- `Authorization: Bearer <access_token>` (from `/api/auth/login`)
- **Scoped API keys** (admin-created; sent via `X-Api-Key: dp_...`)

---

## Roles (RBAC)

- **viewer**: list + playback + read-only
- **operator**: submit jobs + cancel
- **editor**: edit/review/overrides (but not submit jobs)
- **admin**: settings, keys, session management, kill/delete

---

## Submit a job (phone-friendly)

### Upload Wizard
Open:
- `/ui/upload`

Step 1: pick a file
- Upload a file from your phone/computer, or
- Choose a **server-local file** (restricted to `APP_ROOT/Input`)

Step 2: settings
- **Library metadata (required)**:
  - Series name (text)
  - Season (text; accepts `1`, `01`, `Season 1`, `S1`)
  - Episode (text; accepts `4`, `04`, `Episode 4`, `E4`)
- Mode (high/medium/low)
- Project/profile (optional)
- Retention/cache policy (full/balanced/minimal)
- Optional imports (SRT/JSON) to skip ASR/translation when possible

Step 3: voice
- Choose voice mode/preset behavior (varies by configured TTS)

The server will create a job and you’ll be redirected to its page.

#### Upload progress + resume (mobile)
- A progress card appears during upload showing:
  - bytes uploaded / total
  - % complete
  - chunk X/Y
  - speed + ETA
- If the upload can resume (same file + valid session), the UI shows **Resuming upload…**.
- If resume is not possible, the UI explains why (session expired or file changed) and starts fresh.

Screenshots (capture for your deployment):
- Upload progress card with bytes/%/speed.
- “Resuming upload…” banner after refresh.

Known limits:
- File size caps are enforced (`MAX_UPLOAD_BYTES`, `MAX_UPLOAD_BYTES_PER_USER`).
- Chunk size is server-defined (`UPLOAD_CHUNK_BYTES`) and may vary by deployment.
- Resumable uploads require the same file on the same device; changing files resets the session.

---

## Browse the grouped Library (mobile-friendly)

Open:
- `/ui/library`

Pages:
- `/ui/library`: series list (search + My/Public toggle)
- `/ui/library/<series_slug>`: seasons
- `/ui/library/<series_slug>/season/<season_number>`: episodes
- `/ui/library/<series_slug>/season/<season_number>/episode/<episode_number>`: playback + versions

Notes:
- Ordering is server-provided (numeric season/episode sorting).
- Object-level auth is enforced server-side:
  - you only see your private jobs, plus other users’ public jobs (admin sees all).

### Resumable chunked uploads (API)
The server supports resumable uploads:
- `POST /api/uploads/init`
- `POST /api/uploads/{upload_id}/chunk`
- `POST /api/uploads/{upload_id}/complete`

The UI uses this under the hood for reliability on mobile networks.

---

## Monitor progress and logs

From a job page (`/ui/jobs/<job_id>`):
- **Overview**: state, progress, stage breakdown
- **Logs**: live-ish tail (SSE) + downloadable logs

You can also use API:
- `GET /api/jobs/<job_id>`
- `GET /api/jobs/<job_id>/logs/tail?n=200`

Cancel:
- UI button or `POST /api/jobs/<job_id>/cancel`

Admin-only:
- `POST /api/jobs/<job_id>/kill` (force stop)
- `DELETE /api/jobs/<job_id>` (delete job + output dir)

---

## Map speakers to characters (Voices tab)

From a job page (`/ui/jobs/<job_id>`):
- Open the **Voices** tab.
- Each speaker shows an extracted ref preview (if allowed by privacy settings).
- Select a character from the dropdown and click **Save mapping** to lock it.
- Click **Promote to series voice** to store the ref in the series voice store.

Optional suggestions:
- Enable `VOICE_AUTO_MATCH=1` to show **Suggested** mappings with confidence.

Two-pass note:
- If you update mappings and want them applied to cloning, rerun **pass 2** (admin button).

---

## Playback (mobile-safe)

The job page “Playback” section will select the best available option:
- **Mobile MP4**: `Output/<stem>/mobile/mobile.mp4` (best for iOS/Android; enabled by default via `MOBILE_OUTPUTS=1`)
- **Optional HLS**: `Output/<stem>/mobile/hls/index.m3u8` (when enabled via `MOBILE_HLS=1`)
- **Master**: MKV/MP4 outputs

Tips:
- iOS Safari often struggles with MKV. Use **mobile.mp4**, **HLS**, or the “Open in VLC” links.
- HTTP Range requests are supported for efficient seeking.

---

## QA → Fix loop (actionable on phone)

If QA is enabled for a job:
- The “Quality” tab shows a score and top issues.
- Tap **Fix** to open the “Review / Edit” tab focused on the flagged segment.

In “Review / Edit”:
- edit target text
- regen audio
- preview audio
- lock/unlock segments
- optional quick helpers (shorten/formal/reduce slang/PG), best-effort

Overrides:
- music region adjustments
- speaker override dropdown per segment

---

## Optional: WebRTC preview

If WebRTC dependencies are installed (`.[webrtc]`):
- `POST /webrtc/offer` is available (auth required; rate-limited)
- `/webrtc/demo` is an authenticated demo page

If deps are missing, the server returns `503 WebRTC deps not installed`.

---

## Remote access from a phone (opt-in)

### Recommended: Tailscale (private)

```bash
export REMOTE_ACCESS_MODE=tailscale
export HOST=0.0.0.0
export PORT=8000
dubbing-web
```

Then run:

```bash
python3 scripts/remote/tailscale_check.py
```

Open the printed URL on your phone.

### Optional: Cloudflare Tunnel + Access

```bash
export REMOTE_ACCESS_MODE=cloudflare
export TRUST_PROXY_HEADERS=1
export HOST=0.0.0.0
export PORT=8000
dubbing-web
```

Then follow `docs/remote_access.md` for tunnel + Access setup.

---

## Optional: private job notifications (ntfy)

If you run a private self-hosted ntfy server, the pipeline can send job completion/failure notifications.

Docs:
- `docs/notifications.md`

Verify safely (passes even if ntfy is not configured):

```bash
python3 scripts/verify_ntfy.py
```

