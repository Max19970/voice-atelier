"""Real Qwen3-TTS adapter. Loads only local pinned model directories."""

from __future__ import annotations

import gc
import time
from pathlib import Path
from typing import Any

from ..audio import TARGET_SR, atempo, write_wav
from ..config import LANGUAGE_NAMES, Settings
from ..errors import EngineCancelled, EngineError
from .base import CancelFn, ProgressFn, SynthRequest, SynthResult


class QwenEngine:
    """Wraps qwen_tts.Qwen3TTSModel; imports torch/qwen_tts lazily."""

    engine_name = "qwen3-tts"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._model: Any = None
        self._loaded: str | None = None
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    # ---- device/status -------------------------------------------------
    def _resolve_device(self) -> tuple[str, bool, list[str]]:
        import torch  # noqa: PLC0415 - optional GPU dependency

        cuda_ok = bool(torch.cuda.is_available())
        warnings: list[str] = []
        if self.settings.device == "cpu":
            return "cpu", False, warnings
        if self.settings.device == "cuda":
            if not cuda_ok:
                warnings.append("CUDA requested but unavailable; load will fail")
                return "cpu", False, warnings
            return "cuda:0", True, warnings
        if cuda_ok:
            return "cuda:0", True, warnings
        warnings.append("CUDA unavailable: running on CPU (much slower, float32)")
        return "cpu", False, warnings

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._status_cache and now - self._status_cache[0] < 30.0:
            return self._status_cache[1]
        info: dict[str, Any] = {
            "available": False,
            "engine": self.engine_name,
            "loaded_model": self._loaded,
            "device": {"name": "cpu", "vram_mb": None, "cuda": False},
            "warnings": [],
        }
        try:
            import qwen_tts  # noqa: F401,PLC0415
        except Exception as exc:  # pragma: no cover - depends on environment
            info["warnings"].append(f"qwen-tts is not installed: {exc}")
            self._status_cache = (now, info)
            return info
        try:
            import torch  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - depends on environment
            info["warnings"].append(f"PyTorch is not installed: {exc}")
            self._status_cache = (now, info)
            return info
        device, cuda, warnings = self._resolve_device()
        info["warnings"].extend(warnings)
        info["device"]["cuda"] = cuda
        info["device"]["name"] = "cpu" if not cuda else str(torch.cuda.get_device_name(0))
        if cuda:
            try:
                props = torch.cuda.get_device_properties(0)
                info["device"]["vram_mb"] = int(props.total_memory // (1024 * 1024))
            except Exception:
                info["device"]["vram_mb"] = None
        info["available"] = not (self.settings.device == "cuda" and not cuda)
        info["selected_device"] = device
        self._status_cache = (now, info)
        return info

    # ---- lifecycle -----------------------------------------------------
    def load(self, model_id: str, path: Path, mode: str) -> dict[str, Any]:
        try:
            import torch  # noqa: PLC0415
            from qwen_tts import Qwen3TTSModel  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - environment dependent
            raise EngineError(
                "engine_unavailable", f"qwen-tts/PyTorch are not installed: {exc}"
            ) from exc

        if self._model is not None:
            self.unload()
        device, cuda, warnings = self._resolve_device()
        if self.settings.device == "cuda" and not cuda:
            raise EngineError("cuda_unavailable", "CUDA was requested but is not available")
        dtype = torch.bfloat16 if cuda else torch.float32
        try:
            model = Qwen3TTSModel.from_pretrained(
                str(path),
                device_map=device,
                dtype=dtype,
                attn_implementation="sdpa",
                local_files_only=True,
            )
        except torch.cuda.OutOfMemoryError as exc:  # type: ignore[attr-defined]
            raise EngineError("gpu_oom", f"GPU out of memory while loading model: {exc}") from exc
        except Exception as exc:
            message = str(exc)
            if "out of memory" in message.lower():
                raise EngineError("gpu_oom", f"GPU out of memory while loading model: {message}") from exc
            raise EngineError("engine_load_failed", f"Could not load {model_id}: {message}") from exc
        self._model = model
        self._loaded = model_id
        self._status_cache = None
        return {
            "model_id": model_id,
            "mode": mode,
            "device": device,
            "dtype": "bfloat16" if cuda else "float32",
            "warnings": warnings,
        }

    def unload(self) -> None:
        model = self._model
        self._model = None
        self._loaded = None
        self._status_cache = None
        if model is not None:
            del model
            gc.collect()
            try:
                import torch  # noqa: PLC0415

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

    # ---- synthesis -----------------------------------------------------
    def synthesize(
        self, request: SynthRequest, progress: ProgressFn, cancelled: CancelFn
    ) -> SynthResult:
        if self._model is None or self._loaded != request.model_id:
            raise EngineError(
                "model_not_loaded",
                f"Model {request.model_id} is not loaded",
            )
        if cancelled():
            raise EngineCancelled()
        language = LANGUAGE_NAMES.get(request.language)
        if language is None:
            raise EngineError("unsupported_language", f"Unsupported language: {request.language}")
        try:
            import numpy as np  # noqa: PLC0415
            import torch  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover
            raise EngineError("engine_unavailable", f"Engine dependencies missing: {exc}") from exc

        from ..textutil import split_chunks  # noqa: PLC0415

        chunks = split_chunks(request.text, request.chunk_chars)
        if not chunks:
            raise EngineError("empty_text", "Nothing to synthesize")
        if request.mode == "design" and len(chunks) > 1:
            raise EngineError(
                "design_text_too_long",
                "Voice design supports one identity per call (max 240 characters); "
                "save the designed voice and clone longer text with a Base model",
            )

        prompt_items = None
        if request.mode == "clone":
            if not request.ref_audio_path or not request.ref_text:
                raise EngineError(
                    "missing_reference",
                    "Clone mode needs a reference clip and transcript",
                )
            progress(0.02, "preparing voice reference")
            try:
                # Encode the reference once per request, not once per chunk.
                prompt_items = self._model.create_voice_clone_prompt(
                    ref_audio=str(request.ref_audio_path),
                    ref_text=request.ref_text,
                    x_vector_only_mode=False,
                )
            except EngineError:
                raise
            except Exception as exc:
                raise EngineError(
                    "reference_encode_failed",
                    f"Could not encode the reference voice: {exc}",
                ) from exc

        audios: list[Any] = []
        warnings: list[str] = []
        for index, chunk in enumerate(chunks):
            if cancelled():
                raise EngineCancelled()
            torch.manual_seed((int(request.seed) + index) % (2**31))
            # Bound decoding per chunk; long text is split instead of truncated.
            max_new_tokens = min(2048, max(128, int(len(chunk) * 5)))
            try:
                if request.mode == "clone":
                    wavs, sample_rate = self._model.generate_voice_clone(
                        text=chunk,
                        language=language,
                        voice_clone_prompt=prompt_items,
                        max_new_tokens=max_new_tokens,
                    )
                elif request.mode == "design":
                    wavs, sample_rate = self._model.generate_voice_design(
                        text=chunk,
                        language=language,
                        instruct=request.instruction,
                        max_new_tokens=max_new_tokens,
                    )
                else:
                    raise EngineError("invalid_mode", f"Unknown mode: {request.mode}")
            except EngineCancelled:
                raise
            except EngineError:
                raise
            except Exception as exc:
                message = str(exc)
                if "out of memory" in message.lower():
                    raise EngineError("gpu_oom", f"GPU out of memory during synthesis: {message}") from exc
                raise EngineError("synthesis_failed", f"Generation failed: {message}") from exc
            if not wavs:
                raise EngineError("empty_output", "Engine returned no audio")
            audio = np.asarray(wavs[0], dtype=np.float32).reshape(-1)
            if audio.size == 0:
                raise EngineError("empty_output", "Engine returned empty audio")
            chunk_duration = float(audio.size) / float(sample_rate)
            # Runaway/repetition guard: much longer than any plausible reading of
            # the chunk means the decoder repeated, not that we should cut audio.
            if chunk_duration > max(2.5, len(chunk) / 6.0 + 4.0):
                warnings.append("possible_runaway_repetition")
            audios.append(audio)
            progress((index + 1) / len(chunks), f"generating {index + 1}/{len(chunks)}")

        audio = np.concatenate(audios)
        if not np.isfinite(audio).all():
            raise EngineError("non_finite_output", "Engine produced NaN/Inf samples")
        # Cooperative cancel after the last (potentially uninterruptible) GPU
        # call must win before anything is written or committed.
        if cancelled():
            raise EngineCancelled()
        raw_path = request.out_path
        rate_applied = 1.0
        if abs(float(request.rate) - 1.0) > 0.005:
            tmp_raw = request.out_path.with_suffix(".raw.wav")
            write_wav(tmp_raw, audio, sample_rate)
            atempo(tmp_raw, request.out_path, float(request.rate), timeout=self.settings.subprocess_timeout)
            try:
                tmp_raw.unlink()
            except OSError:
                pass
            rate_applied = float(request.rate)
            # Duration is re-measured after the post-process.
            from ..audio import duration_seconds, load_audio  # noqa: PLC0415

            processed, processed_sr = load_audio(request.out_path)
            audio = processed
            sample_rate = processed_sr
            duration = duration_seconds(audio, sample_rate)
            if not np.isfinite(audio).all():
                raise EngineError("non_finite_output", "Rate post-process produced invalid samples")
        else:
            write_wav(raw_path, audio, sample_rate)
            duration = float(audio.size) / float(sample_rate)

        if sample_rate != TARGET_SR:
            warnings.append(f"engine_sample_rate_{sample_rate}")
        expected = len(request.text) / 25.0
        if duration < max(0.4, expected * 0.45) and len(request.text) > 60:
            warnings.append("possible_truncation_heuristic")
        if duration > len(request.text) / 6.0 + 6.0:
            warnings.append("possible_runaway_repetition")
        return SynthResult(
            sample_rate=int(sample_rate),
            duration=round(float(duration), 3),
            chunks=len(chunks),
            warnings=warnings,
            rate_applied=rate_applied,
            engine_info={
                "engine": self.engine_name,
                "model_id": request.model_id,
                "device": "cuda" if self.settings.device != "cpu" else "cpu",
                "seed_policy": "torch.manual_seed(seed + chunk_index); best effort, not bit-exact",
            },
        )
