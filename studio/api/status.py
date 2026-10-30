"""Status, model catalog and engine lifecycle endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import __version__
from ..config import DESIGN_MAX_CHARS, SUPPORTED_LANGUAGES
from ..engine.catalog import MODELS, installed_state
from ..engine.downloader import read_manifest
from ..errors import ApiError
from ..schemas import DownloadRequest
from ..security import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.get("/status")
def status(request: Request) -> dict:
    settings = request.app.state.settings
    manager = request.app.state.manager
    engine = manager.status()
    warnings = list(engine.get("warnings") or [])
    if settings.offline:
        warnings.append("offline mode: downloads and Hugging Face network access are disabled")
    return {
        "version": __version__,
        "offline": settings.offline,
        "engine_available": bool(engine.get("available")),
        "device": engine.get("device") or {"name": "cpu", "vram_mb": None, "cuda": False},
        "loaded_model": manager.loaded_model,
        "capabilities": {
            "languages": list(SUPPORTED_LANGUAGES),
            "clone_instruction": False,
            "design_instruction": True,
            "rate": {"min": 0.5, "max": 2.0, "mode": "postprocess"},
            "seed": True,
            "max_design_chars": DESIGN_MAX_CHARS,
        },
        "paths": {"data": str(settings.home)},
        "warnings": warnings,
    }


@router.get("/models")
def models(request: Request) -> dict:
    settings = request.app.state.settings
    items = []
    for spec in MODELS:
        installed, status_text, missing = installed_state(settings, spec)
        manifest = read_manifest(settings, spec.id) if installed else None
        items.append(
            {
                "id": spec.id,
                "label": spec.label,
                "repo": spec.repo,
                "revision": spec.revision,
                "kind": spec.kind,
                "size_gb": spec.size_gb,
                "license": spec.license,
                "installed": installed,
                "status": "installed" if manifest else status_text,
                "description": spec.description,
            }
        )
    return {"items": items}


@router.post("/models/{model_id}/download")
def download(request: Request, model_id: str, body: DownloadRequest | None = None) -> dict:
    settings = request.app.state.settings
    spec = next((item for item in MODELS if item.id == model_id), None)
    if spec is None:
        raise ApiError(404, "not_found", "Unknown model")
    if settings.offline:
        raise ApiError(409, "offline", "Downloads are disabled by VOICE_ATELIER_OFFLINE")
    if body is None or not body.accept_license:
        raise ApiError(400, "license_required", "License acceptance is required before downloading")
    runner = request.app.state.runner
    return runner.enqueue("download", {"model_id": model_id})


@router.post("/engine/unload")
def unload(request: Request) -> dict:
    runner = request.app.state.runner
    return runner.enqueue("unload", {})
