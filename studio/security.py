"""Localhost security: Host allowlist, same-origin mutations, per-launch token, CSP."""

from __future__ import annotations

import hmac
import html
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import Settings
from .errors import ApiError, error_payload

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "font-src 'self'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def new_token() -> str:
    return secrets.token_urlsafe(32)


async def require_token(request: Request) -> None:
    expected = request.app.state.token
    supplied = request.headers.get("x-studio-token", "")
    # compare_digest rejects non-ASCII str inputs; compare UTF-8 bytes and bound length.
    ok = (
        0 < len(supplied) <= 256
        and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    )
    if not ok:
        raise ApiError(401, "unauthorized", "Missing or invalid X-Studio-Token header")


def install_middleware(app, settings: Settings) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").strip().lower()
        if host not in settings.allowed_hosts:
            # Blocks DNS-rebinding and non-loopback names.
            return JSONResponse(
                error_payload("invalid_host", "Request Host is not allowed"),
                status_code=400,
            )
        if request.method in MUTATING_METHODS:
            origin = (request.headers.get("origin") or "").strip().rstrip("/")
            if origin and origin.lower() not in settings.allowed_origins:
                return JSONResponse(
                    error_payload("invalid_origin", "Cross-origin requests are not allowed"),
                    status_code=400,
                )
            content_type = (request.headers.get("content-type") or "").lower()
            if not content_type.startswith("multipart/"):
                try:
                    declared = int(request.headers.get("content-length") or "0")
                except ValueError:
                    declared = 0
                if declared > settings.max_json_bytes:
                    return JSONResponse(
                        error_payload("payload_too_large", "JSON body is too large"),
                        status_code=413,
                    )
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def render_index(settings: Settings) -> str:
    """Serve web/index.html with the per-launch token injected (HTML-escaped)."""
    index_path = Path(settings.web_dir) / "index.html"
    token = html.escape(settings.token, quote=True)
    if index_path.is_file():
        text = index_path.read_text(encoding="utf-8")
        if "__STUDIO_TOKEN__" in text:
            return text.replace("__STUDIO_TOKEN__", token)
        return text
    return (
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='studio-token' content='" + token + "'>"
        "<title>Voice Atelier</title></head><body>"
        "<h1>Voice Atelier</h1>"
        "<p>Web interface is not installed yet (web/index.html is missing).</p>"
        "</body></html>"
    )
