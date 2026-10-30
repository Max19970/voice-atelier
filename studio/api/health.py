"""Unauthenticated liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from .. import __version__

router = APIRouter()


@router.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": __version__}
