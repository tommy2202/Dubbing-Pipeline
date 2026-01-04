## Model management (cache status + optional prewarm)

The model management page shows what heavy ML models are currently loaded and basic disk usage information. It also supports an **optional** “prewarm” action to download/prepare models.

### Where it lives
- **UI**: `/ui/models` (admin only)
- **API**:
  - `GET /api/runtime/models` (admin only)
  - `POST /api/runtime/models/prewarm?preset=low|medium|high` (admin only)

### Default-off behavior (no accidental internet)
Prewarm/download is **blocked** unless both are enabled:
- `ENABLE_MODEL_DOWNLOADS=1`
- `ALLOW_EGRESS=1` (or equivalent egress policy allowing downloads)

If disabled, the UI still shows the cache state and the API returns a hint explaining how to enable.

### Verify (synthetic)
```bash
python3 scripts/verify_model_manager.py
```

