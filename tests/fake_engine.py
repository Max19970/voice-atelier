"""Deterministic test adapter. Tests only — never imported by production code."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from studio.config import Settings
from studio.engine.base import SynthRequest, SynthResult
from studio.errors import EngineCancelled
from studio.textutil import split_chunks


class FakeEngine:
    """Writes a deterministic low-level tone; no silence fallback in production."""

    engine = "fake-test"

    _guard = threading.Lock()
    active = 0
    max_active = 0
    load_count = 0

    def __init__(self, settings: Settings, delay: float = 0.05, design_duration: float = 2.5) -> None:
        self.settings = settings
        self.delay = delay
        # Design references must be long enough to be reusable as a voice;
        # clone takes stay short so existing mix-duration tests remain stable.
        self.design_duration = design_duration
        self.loaded: str | None = None
        self.load_error: str | None = None

    # ---- protocol ------------------------------------------------------
    def status(self) -> dict:
        return {
            "available": self.load_error is None,
            "engine": self.engine,
            "loaded_model": self.loaded,
            "device": {"name": "cpu", "vram_mb": 0, "cuda": False},
            "warnings": [] if self.load_error is None else [self.load_error],
        }

    def load(self, model_id: str, path: Path, mode: str) -> dict:
        type(self).load_count += 1
        if self.load_error:
            from studio.errors import EngineError

            raise EngineError("engine_load_failed", self.load_error)
        self.loaded = model_id
        return {"model_id": model_id, "device": "cpu"}

    def unload(self) -> None:
        self.loaded = None

    def synthesize(self, request: SynthRequest, progress, cancelled) -> SynthResult:
        chunks = split_chunks(request.text, request.chunk_chars)
        if not chunks:
            from studio.errors import EngineError

            raise EngineError("empty_text", "Nothing to synthesize")
        with FakeEngine._guard:
            FakeEngine.active += 1
            FakeEngine.max_active = max(FakeEngine.max_active, FakeEngine.active)
        try:
            sample_rate = 24000
            pieces = []
            for index, chunk in enumerate(chunks):
                if cancelled():
                    raise EngineCancelled()
                time.sleep(self.delay)
                if cancelled():
                    raise EngineCancelled()
                duration = self.design_duration if request.mode == "design" else 0.25
                t = np.arange(int(duration * sample_rate)) / sample_rate
                pieces.append((0.1 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
                progress((index + 1) / len(chunks), f"generating {index + 1}/{len(chunks)}")
            audio = np.concatenate(pieces)
            path = Path(request.out_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            sf.write(str(path), audio, sample_rate, subtype="PCM_16")
            return SynthResult(
                sample_rate=sample_rate,
                duration=round(len(audio) / sample_rate, 3),
                chunks=len(chunks),
                warnings=[],
                engine_info={"engine": self.engine},
            )
        finally:
            with FakeEngine._guard:
                FakeEngine.active -= 1
