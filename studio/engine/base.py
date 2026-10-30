"""Engine adapter protocol shared by the real Qwen adapter and test doubles."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


@dataclass
class SynthRequest:
    text: str
    language: str
    mode: str  # "clone" | "design"
    model_id: str
    out_path: Path
    seed: int = 42
    rate: float = 1.0
    instruction: str = ""
    ref_audio_path: Path | None = None
    ref_text: str | None = None
    chunk_chars: int = 240


@dataclass
class SynthResult:
    sample_rate: int
    duration: float
    chunks: int
    warnings: list[str] = field(default_factory=list)
    rate_applied: float = 1.0
    engine_info: dict[str, Any] = field(default_factory=dict)


class EngineProtocol(Protocol):
    def status(self) -> dict[str, Any]: ...

    def load(self, model_id: str, path: Path, mode: str) -> dict[str, Any]: ...

    def unload(self) -> None: ...

    def synthesize(
        self, request: SynthRequest, progress: ProgressFn, cancelled: CancelFn
    ) -> SynthResult: ...
