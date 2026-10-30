"""Job polling and cancellation."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..security import require_token

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.get("/jobs")
def list_jobs(request: Request) -> dict:
    return {"items": request.app.state.runner.list()}


@router.get("/jobs/{job_id}")
def get_job(request: Request, job_id: str) -> dict:
    return request.app.state.runner.get(job_id)


@router.post("/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: str) -> dict:
    return request.app.state.runner.cancel(job_id)
