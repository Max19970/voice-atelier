"""Take audio download."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from ..security import require_token
from ..services import projects

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.get("/takes/{take_id}/audio")
def take_audio(request: Request, take_id: str):
    path = projects.take_audio_path(request.app.state.db, take_id)
    return FileResponse(
        str(path), media_type="audio/wav", headers={"Cache-Control": "no-store"}
    )
