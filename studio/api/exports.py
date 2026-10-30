"""Export download."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from ..security import require_token
from ..services import export as export_service

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

MEDIA_TYPES = {"wav": "audio/wav", "zip": "application/zip"}


@router.get("/exports/{export_id}/download")
def download_export(request: Request, export_id: str):
    path, fmt, filename = export_service.export_path(request.app.state.db, export_id)
    return FileResponse(
        str(path),
        media_type=MEDIA_TYPES.get(fmt, "application/octet-stream"),
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )
