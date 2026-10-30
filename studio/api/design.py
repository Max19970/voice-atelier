"""Voice Design: new synthetic identities from a description."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..config import DESIGN_MAX_CHARS, SUPPORTED_LANGUAGES
from ..engine.catalog import DEFAULT_DESIGN_MODEL, MODELS_BY_ID, installed_state
from ..errors import ApiError
from ..schemas import DesignRequest
from ..security import require_token
from ..services import projects

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.get("/designs")
def list_designs(request: Request) -> dict:
    return {"items": projects.list_designs(request.app.state.db)}


@router.post("/design")
def design(request: Request, body: DesignRequest) -> dict:
    settings = request.app.state.settings
    text = (body.text or "").strip()
    instruction = (body.instruction or "").strip()
    if not body.consent:
        raise ApiError(422, "consent_required", "Rights acknowledgement is required")
    if not text:
        raise ApiError(422, "empty_text", "Text is required")
    if len(text) > DESIGN_MAX_CHARS:
        raise ApiError(
            422,
            "design_text_too_long",
            f"Voice design is limited to {DESIGN_MAX_CHARS} characters per call because each "
            "call generates a new identity; save the designed voice as a voice, then clone "
            "longer text with a Base model",
        )
    if len(text) > settings.max_text_chars:
        raise ApiError(422, "text_too_long", "Text is too long")
    if not instruction:
        raise ApiError(422, "missing_instruction", "Instruction describing the new voice is required")
    if len(instruction) > settings.max_instruction_chars:
        raise ApiError(422, "instruction_too_long", "Instruction is too long")
    if body.language not in SUPPORTED_LANGUAGES:
        raise ApiError(422, "invalid_language", "Unsupported language")
    if not (0 <= int(body.seed) <= 2**31 - 1):
        raise ApiError(422, "invalid_seed", "seed must be within 0..2^31-1")
    if not (0.5 <= float(body.rate) <= 2.0):
        raise ApiError(422, "invalid_rate", "rate must be within 0.5..2")
    if body.save_as_voice and not (body.name or "").strip():
        raise ApiError(422, "invalid_name", "A name is required to save the designed voice")

    spec = MODELS_BY_ID[DEFAULT_DESIGN_MODEL]
    installed, _status, missing = installed_state(settings, spec)
    if not installed:
        raise ApiError(
            409,
            "model_not_installed",
            f"Model {spec.id} is not installed",
            {"model_id": spec.id, "missing": missing},
        )
    payload = {
        "model_id": spec.id,
        "text": text,
        "instruction": instruction,
        "language": body.language,
        "seed": int(body.seed),
        "rate": float(body.rate),
        "save_as_voice": bool(body.save_as_voice),
        "name": (body.name or "").strip()[:120],
        "consent": True,
    }
    return request.app.state.runner.enqueue("design", payload)
