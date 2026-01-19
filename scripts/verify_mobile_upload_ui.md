# Verify mobile upload UX (manual)

Goal: confirm progress, retries, and resume UX without changing the upload protocol.

## Setup
1) Start the web server (`dubbing-web`) with a test user.
2) Open `/ui/upload` on a phone or mobile-sized browser window.

## Steps
1) Choose a small MP4 (1–5 MB) and click **Start Job**.
2) Confirm the upload panel appears and shows:
   - bytes uploaded / total
   - % complete
   - chunk X/Y
   - speed + ETA
3) Simulate a transient failure:
   - Toggle airplane mode for a few seconds, then resume.
   - Verify the UI shows a retry message and continues.
4) Refresh the page mid-upload:
   - Re-select the same file.
   - Verify the UI shows **Resuming upload…** and continues.
5) Complete the upload and verify job creation redirects to `/ui/jobs/<id>`.

## Expected results
- Progress and chunk counters update smoothly.
- Retry messages are clear and do not expose stack traces.
- Resume is explicit when possible and explains why when not possible.
- Errors are actionable and mobile-friendly.
