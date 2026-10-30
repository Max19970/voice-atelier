"""Engine layer: protocol, pinned catalog, Qwen adapter, downloader, manager."""

from .base import EngineProtocol, SynthRequest, SynthResult
from .catalog import (
    CLONE_MODEL_IDS,
    DEFAULT_CLONE_MODEL,
    DEFAULT_DESIGN_MODEL,
    DESIGN_MODEL_IDS,
    MODELS,
    MODELS_BY_ID,
    ModelSpec,
    installed_state,
    model_dir,
)
from .manager import EngineManager

__all__ = [
    "EngineProtocol",
    "SynthRequest",
    "SynthResult",
    "MODELS",
    "MODELS_BY_ID",
    "ModelSpec",
    "CLONE_MODEL_IDS",
    "DESIGN_MODEL_IDS",
    "DEFAULT_CLONE_MODEL",
    "DEFAULT_DESIGN_MODEL",
    "installed_state",
    "model_dir",
    "EngineManager",
]
