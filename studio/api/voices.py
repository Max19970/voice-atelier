"""Voice library endpoints."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ..config import SUPPORTED_LANGUAGES
from ..errors import ApiError
from ..schemas import VoicePatch
from ..security import require_token
from ..services import voices
from ..storage import safe_unlink

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])

TRUE_VALUES = {"1", "true", "yes", "on"}


def _as_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in TRUE_VALUES


async def _save_bounded(upload: UploadFile, destination: Path, max_bytes: int) -> int:
    total = 0
    with open(destination, "wb") as handle:
        while True:
            chunk = await upload.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ApiError(413, "upload_too_large", "Upload exceeds the 50 MB limit")
            handle.write(chunk)
    if total == 0:
        raise ApiError(422, "upload_empty", "Uploaded file is empty")
    return total


@router.get("/voices")
def list_voices(request: Request) -> dict:
    return {"items": voices.list_voices(request.app.state.db)}


@router.post("/voices")
async def create_voice(
    request: Request,
    file: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
    name: str = Form(...),
    language: str = Form("ru"),
    transcript: str = Form(""),
    consent: str = Form("false"),
    consent_basis: str = Form("own"),
    trim_start: float = Form(0.0),
    trim_end: str | None = Form(None),
):
    settings = request.app.state.settings
    db = request.app.state.db
    upload = file or audio
    if upload is None:
        raise ApiError(422, "missing_audio", "An audio file is required")
    if language not in SUPPORTED_LANGUAGES:
        raise ApiError(422, "invalid_language", "Unsupported language")
    suffix = Path(upload.filename or "").suffix[:10]
    temp_path = settings.tmp_dir / f"upload-{uuid.uuid4().hex}{suffix}"
    parsed_end = None
    if trim_end not in (None, ""):
        try:
            parsed_end = float(trim_end)
        except (TypeError, ValueError):
            raise ApiError(422, "invalid_trim", "trim_end must be a number")
    try:
        await _save_bounded(upload, temp_path, settings.max_upload_bytes)
        voice = await run_in_threadpool(
            voices.create_from_upload,
            settings,
            db,
            temp_path,
            name=name,
            language=language,
            transcript=transcript,
            consent=_as_bool(consent),
            consent_basis=consent_basis.strip(),
            trim_start=trim_start,
            trim_end=parsed_end,
        )
        return voice
    finally:
        safe_unlink(temp_path)


@router.patch("/voices/{voice_id}")
def patch_voice(request: Request, voice_id: str, body: VoicePatch) -> dict:
    return voices.update_voice(
        request.app.state.db, voice_id, name=body.name, transcript=body.transcript
    )


@router.get("/voices/{voice_id}/audio")
def voice_audio(request: Request, voice_id: str):
    path = voices.audio_path(request.app.state.db, voice_id)
    return FileResponse(
        str(path),
        media_type="audio/wav",
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/voices/{voice_id}")
def delete_voice(request: Request, voice_id: str) -> dict:
    voices.delete_voice(request.app.state.settings, request.app.state.db, voice_id)
    return {"ok": True}
