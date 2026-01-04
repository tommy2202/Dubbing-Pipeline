## Library management (jobs list, tags, archive, delete)

The “Library” is the jobs list + filtering features in the v2 web UI.

### Where it lives
- **UI**: `/ui/` dashboard (jobs list/cards)
- **API**: `/api/jobs` (list), `/api/jobs/{id}` (detail)

### Filters
Jobs can be filtered by:
- **status**
- **project**
- **mode**
- **tag**
- **archived** (include/exclude)

These filters are exposed in the dashboard UI and in the `GET /api/jobs` query params.

### Tags
- Set tags (operator/admin):
  - `PUT /api/jobs/{id}/tags` with JSON body `{"tags": ["tag1","tag2"]}`

### Archive / unarchive
- Archive (operator/admin):
  - `POST /api/jobs/{id}/archive`
- Unarchive (operator/admin):
  - `POST /api/jobs/{id}/unarchive`

Archived jobs are hidden by default unless “include archived” is enabled.

### Delete
- Delete (admin only):
  - `DELETE /api/jobs/{id}`

Delete attempts to stop the job first, then removes the job record and its output directory using path safety checks.

### Verify (synthetic)
```bash
python3 scripts/verify_library_ops.py
```

