"""Serial engine lifecycle: one resident model at a time."""

from __future__ import annotations

import threading
from typing import Any, Callable

from ..config import Settings
from ..errors import EngineError
from .catalog import MODELS_BY_ID, installed_state, model_dir
from .downloader import read_manifest
from .qwen import QwenEngine

EngineFactory = Callable[[Settings], Any]


class EngineManager:
    def __init__(self, settings: Settings, engine_factory: EngineFactory | None = None) -> None:
        self.settings = settings
        self._factory = engine_factory
        self._adapter: Any = None
        self._loaded: str | None = None
        self._lock = threading.RLock()

    def adapter(self) -> Any:
        with self._lock:
            if self._adapter is None:
                self._adapter = (
                    self._factory(self.settings) if self._factory else QwenEngine(self.settings)
                )
            return self._adapter

    @property
    def loaded_model(self) -> str | None:
        return self._loaded

    def status(self) -> dict[str, Any]:
        try:
            info = dict(self.adapter().status())
        except Exception as exc:  # never let status break the UI
            info = {
                "available": False,
                "engine": "unknown",
                "device": {"name": "cpu", "vram_mb": None, "cuda": False},
                "warnings": [f"engine status failed: {exc}"],
            }
        info["loaded_model"] = self._loaded
        return info

    def ensure(self, model_id: str) -> dict[str, Any]:
        spec = MODELS_BY_ID.get(model_id)
        if spec is None:
            raise EngineError("unknown_model", f"Unknown model id: {model_id}")
        installed, status, missing = installed_state(self.settings, spec)
        if not installed:
            raise EngineError(
                "model_not_installed",
                f"Model {model_id} is not installed or its verified manifest does not match "
                "the pinned revision; reinstall it from the Models page",
                {"model_id": model_id, "status": status, "missing": missing},
            )
        with self._lock:
            if self._loaded == model_id:
                return {"model_id": model_id, "already_loaded": True}
            self._unload_locked()
            info = self.adapter().load(model_id, model_dir(self.settings, model_id), spec.kind)
            self._loaded = model_id
            manifest = read_manifest(self.settings, model_id) or {}
            info["revision"] = manifest.get("revision", spec.revision)
            return info

    def unload(self) -> None:
        with self._lock:
            self._unload_locked()

    def _unload_locked(self) -> None:
        if self._adapter is not None and self._loaded is not None:
            try:
                self._adapter.unload()
            finally:
                self._loaded = None
