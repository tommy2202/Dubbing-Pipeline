from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from dubbing_pipeline.jobs.models import now_utc
from dubbing_pipeline.security.crypto import is_encrypted_path


router = APIRouter()


def record_direct_upload(
    *,
    store: Any,
    upload_id: str,
    user_id: str,
    video_path: Path,
    bytes_written: int,
    filename: str,
) -> None:
    if not upload_id or store is None:
        return
    try:
        store.put_upload(
            upload_id,
            {
                "id": upload_id,
                "owner_id": user_id,
                "filename": filename,
                "orig_stem": Path(filename).stem,
                "total_bytes": int(bytes_written),
                "chunk_bytes": 0,
                "part_path": "",
                "final_path": str(video_path),
                "received": {},
                "received_bytes": int(bytes_written),
                "completed": True,
                "encrypted": bool(is_encrypted_path(video_path)),
                "created_at": now_utc(),
                "updated_at": now_utc(),
                "source": "direct_job",
            },
        )
        store.set_upload_storage_bytes(
            upload_id, user_id=str(user_id), bytes_count=int(bytes_written)
        )
    except Exception:
        return
