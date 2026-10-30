"""Error types shared by API, services, engine and worker."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error that maps to an HTTP error envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class EngineError(Exception):
    """A real engine failure; never used to fake output."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class EngineCancelled(Exception):
    """Raised by an adapter when the cooperatively-observed cancel flag is set."""


class JobCanceled(Exception):
    """Internal control signal used by job handlers."""


def error_payload(
    code: str, message: str, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        body["details"] = details
    return {"error": body}
