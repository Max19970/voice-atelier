"""Project, generation and export endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from ..errors import ApiError
from ..schemas import ExportRequest, GenerateRequest, ProjectCreate, ProjectSave, SelectRequest
from ..security import require_token
from ..services import export as export_service
from ..services import projects

router = APIRouter(prefix="/api", dependencies=[Depends(require_token)])


@router.get("/projects")
def list_projects(request: Request) -> dict:
    return {"items": projects.list_projects(request.app.state.db)}


@router.post("/projects", status_code=201)
def create_project(request: Request, body: ProjectCreate) -> dict:
    return projects.create_project(request.app.state.db, body.title)


@router.get("/projects/{project_id}")
def get_project(request: Request, project_id: str) -> dict:
    return projects.get_project(request.app.state.db, project_id)


@router.put("/projects/{project_id}")
def save_project(request: Request, project_id: str, body: ProjectSave) -> dict:
    settings = request.app.state.settings
    segments = [segment.model_dump() for segment in body.segments]
    return projects.save_project(
        settings, request.app.state.db, project_id, body.title, segments
    )


@router.delete("/projects/{project_id}")
def delete_project(request: Request, project_id: str) -> dict:
    projects.delete_project(
        request.app.state.settings, request.app.state.db, project_id
    )
    return {"ok": True}


@router.post("/projects/{project_id}/generate")
def generate(
    request: Request, project_id: str, body: GenerateRequest | None = None
) -> dict:
    settings = request.app.state.settings
    db = request.app.state.db
    force = bool(body.force) if body else False
    segment_ids = body.segment_ids if body else None
    targets, skipped = projects.build_generation_targets(
        settings, db, project_id, segment_ids, force
    )
    return request.app.state.runner.enqueue(
        "generate",
        {"project_id": project_id, "targets": targets, "skipped": skipped, "force": force},
    )


@router.post("/projects/{project_id}/select")
def select_take(request: Request, project_id: str, body: SelectRequest) -> dict:
    return projects.select_take(
        request.app.state.db, project_id, body.segment_id, body.take_id
    )


@router.post("/projects/{project_id}/export")
def export_project(
    request: Request, project_id: str, body: ExportRequest | None = None
) -> dict:
    settings = request.app.state.settings
    options = body or ExportRequest()
    payload = export_service.build_export_payload(
        settings,
        request.app.state.db,
        project_id,
        options.format,
        options.normalize,
        options.allow_stale,
    )
    return request.app.state.runner.enqueue("export", payload)
