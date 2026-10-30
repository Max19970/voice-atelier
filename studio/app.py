"""FastAPI application factory for Voice Atelier."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import design as design_api
from .api import exports as exports_api
from .api import health as health_api
from .api import jobs as jobs_api
from .api import projects as projects_api
from .api import status as status_api
from .api import takes as takes_api
from .api import voices as voices_api
from .config import Settings
from .db import Database
from .engine.manager import EngineManager
from .errors import ApiError, error_payload
from .jobs import JobRunner
from .security import install_middleware, render_index
from .services.pipeline import Pipeline

log = logging.getLogger("voice_atelier")


def create_app(
    settings: Settings | None = None,
    engine_factory: Callable[[Settings], Any] | None = None,
    web_dir: Path | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    if web_dir is not None:
        settings.web_dir = Path(web_dir)
    settings.ensure_dirs()

    db = Database(settings.db_path)
    db.init()
    manager = EngineManager(settings, engine_factory)
    pipeline = Pipeline(settings, db, manager)
    runner = JobRunner(settings, db, pipeline.handlers)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runner.start()
        try:
            yield
        finally:
            runner.stop()

    app = FastAPI(
        title="Voice Atelier",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.db = db
    app.state.manager = manager
    app.state.pipeline = pipeline
    app.state.runner = runner
    app.state.token = settings.token

    install_middleware(app, settings)

    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            error_payload(exc.code, exc.message, exc.details),
            status_code=exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {"loc": list(item.get("loc", [])), "msg": item.get("msg", ""), "type": item.get("type", "")}
            for item in exc.errors()
        ]
        return JSONResponse(
            error_payload("invalid_request", "Request validation failed", {"errors": errors}),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", exc)
        return JSONResponse(error_payload("internal", "Internal server error"), status_code=500)

    app.include_router(health_api.router)
    app.include_router(status_api.router)
    app.include_router(voices_api.router)
    app.include_router(projects_api.router)
    app.include_router(takes_api.router)
    app.include_router(design_api.router)
    app.include_router(jobs_api.router)
    app.include_router(exports_api.router)

    @app.get("/", include_in_schema=False)
    @app.get("/index.html", include_in_schema=False)
    def index() -> HTMLResponse:
        return HTMLResponse(render_index(settings), headers={"Cache-Control": "no-store"})

    if settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(settings.web_dir), html=False), name="web")

    return app
