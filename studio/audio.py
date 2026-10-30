"""Audio processing: bounded ffmpeg decode, quality metrics, trim, mix, atempo.

No denoise/enhance is applied. Rate change is a pitch-preserving ffmpeg atempo
post-process, always reported as such by callers.
"""

from __future__ import annotations

import math
import re
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import soundfile as sf

from .errors import ApiError
from .storage import sha256_file

TARGET_SR = 24000
_FFMPEG: str | None = None


def ffmpeg_exe() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
    return _FFMPEG


def run_ffmpeg(args: list[str], timeout: int = 240) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_exe(), "-hide_banner", "-nostdin", "-y", *args]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        raise ApiError(422, "audio_processing_failed", "Audio processing timed out")
    except OSError as exc:
        raise ApiError(422, "audio_processing_failed", f"ffmpeg is unavailable: {exc}")
    if proc.returncode != 0:
        raise ApiError(
            422,
            "audio_processing_failed",
            "ffmpeg could not process this audio",
            {"stderr": (proc.stderr or "")[-2000:]},
        )
    return proc


_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_LOOPBACK_PROTOCOLS = "file,pipe"

ALLOWED_AUDIO_SUFFIXES = frozenset(
    {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".webm", ".aac"}
)
_PLAYLIST_MARKERS = (
    b"#extm3u",
    b"#extinf",
    b"[playlist]",
    b"<?xml",
    b"http://",
    b"https://",
    b"file://",
)


def _has_audio_magic(head: bytes) -> bool:
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return True
    if head[:4] == b"fLaC":
        return True
    if head[:4] == b"OggS":
        return True
    if head[:3] == b"ID3":
        return True
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return True  # MPEG audio / ADTS AAC frame sync
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return True  # MP4/M4A container
    if head[:4] == b"\x1aE\xdf\xa3":
        return True  # WebM/Matroska
    return False


def validate_reference_source(path: Path) -> None:
    """Reject playlists/URLs and unknown containers before any ffmpeg parsing.

    A playlist (M3U/PLS/ASX/XSPF/SMIL) can make ffmpeg open local or network
    resources, so this check happens before probe/decode and only real audio
    containers with a sane suffix or magic are accepted.
    """
    with open(path, "rb") as handle:
        head = handle.read(2048)
    suffix = path.suffix.lower()
    if suffix and suffix not in ALLOWED_AUDIO_SUFFIXES:
        raise ApiError(422, "unsupported_audio", f"Unsupported audio extension: {suffix}")
    if _has_audio_magic(head):
        return
    lowered = head.lower()
    if any(marker in lowered for marker in _PLAYLIST_MARKERS):
        raise ApiError(
            422, "unsupported_audio", "Playlists and URLs are not accepted as references"
        )
    raise ApiError(422, "unsupported_audio", "File does not look like a supported audio container")


def probe_duration(src: Path, timeout: int = 60) -> float | None:
    """Best-effort container duration via ffmpeg stderr; None if unknown."""
    cmd = [
        ffmpeg_exe(),
        "-hide_banner",
        "-nostdin",
        "-protocol_whitelist",
        _LOOPBACK_PROTOCOLS,
        "-i",
        str(src),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _DURATION_RE.search(proc.stderr or "")
    if not match:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def decode_to_wav(
    src: Path, dst: Path, timeout: int = 240, max_seconds: float | None = None
) -> None:
    """Decode any supported input to mono 24 kHz PCM16 WAV.

    Only local file/pipe protocols are allowed; no network protocols. When
    max_seconds is set it is a hard safety bound, and callers must still reject
    over-long inputs explicitly instead of relying on it.
    """
    args = ["-protocol_whitelist", _LOOPBACK_PROTOCOLS, "-i", str(src)]
    if max_seconds is not None:
        args.extend(["-t", f"{max_seconds + 0.5:.3f}"])
    args.extend(
        ["-vn", "-ac", "1", "-ar", str(TARGET_SR), "-c:a", "pcm_s16le", str(dst)]
    )
    run_ffmpeg(args, timeout=timeout)


def load_audio(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if isinstance(data, np.ndarray) and data.ndim > 1:
        data = data.mean(axis=1)
    return np.asarray(data, dtype=np.float32), int(sr)


def write_wav(path: Path, audio: np.ndarray, sr: int = TARGET_SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(audio, dtype=np.float32), int(sr), subtype="PCM_16")


def duration_seconds(audio: np.ndarray, sr: int) -> float:
    return float(len(audio)) / float(sr) if sr else 0.0


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(value, 1e-8))


def analyze(audio: np.ndarray, sr: int) -> dict:
    """Objective reference metrics; never claims quality improvement."""
    if len(audio) == 0:
        return {
            "peak_dbfs": -120.0,
            "rms_dbfs": -120.0,
            "clipped_fraction": 0.0,
            "silence_fraction": 1.0,
            "warnings": ["empty_audio"],
        }
    peak = float(np.max(np.abs(audio)))
    rms = float(np.sqrt(np.mean(np.square(audio))))
    clipped = float(np.mean(np.abs(audio) >= 0.999))
    frame = max(int(sr * 0.02), 1)
    usable = (len(audio) // frame) * frame
    if usable >= frame:
        frames = audio[:usable].reshape(-1, frame)
        frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
        silence_fraction = float(np.mean(frame_rms < 10 ** (-50.0 / 20.0)))
    else:
        silence_fraction = 0.0
    duration = duration_seconds(audio, sr)
    warnings: list[str] = []
    if duration < 2.0:
        warnings.append("reference_shorter_than_2s")
    if duration > 30.0:
        warnings.append("reference_longer_than_30s")
    if clipped > 0.001:
        warnings.append("clipping_detected")
    if silence_fraction > 0.6:
        warnings.append("mostly_silence")
    if _dbfs(rms) < -30.0 or _dbfs(peak) < -12.0:
        warnings.append("low_level")
    return {
        "peak_dbfs": round(_dbfs(peak), 2),
        "rms_dbfs": round(_dbfs(rms), 2),
        "clipped_fraction": round(clipped, 6),
        "silence_fraction": round(silence_fraction, 4),
        "warnings": warnings,
    }


def trim_audio(audio: np.ndarray, sr: int, start_s: float, end_s: float | None) -> np.ndarray:
    start = max(0, int(round(start_s * sr)))
    end = len(audio) if end_s is None else min(len(audio), int(round(end_s * sr)))
    if end <= start:
        raise ApiError(422, "invalid_trim", "Trim range is empty")
    return audio[start:end]


def atempo(src: Path, dst: Path, rate: float, timeout: int = 240) -> None:
    if not math.isfinite(rate) or rate < 0.5 or rate > 2.0:
        raise ApiError(422, "invalid_rate", "Rate must be a finite number within 0.5..2.0")
    filters: list[str] = []
    remaining = float(rate)
    # ffmpeg atempo is limited to 0.5..2.0 per filter; chain for wider ranges.
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    filters.append(f"atempo={remaining:.6f}")
    run_ffmpeg(
        [
            "-protocol_whitelist",
            _LOOPBACK_PROTOCOLS,
            "-i",
            str(src),
            "-filter:a",
            ",".join(filters),
            "-ac",
            "1",
            "-ar",
            str(TARGET_SR),
            "-c:a",
            "pcm_s16le",
            str(dst),
        ],
        timeout=timeout,
    )


def silence(seconds: float, sr: int = TARGET_SR) -> np.ndarray:
    return np.zeros(max(0, int(round(seconds * sr))), dtype=np.float32)


def mix_takes(
    take_paths: list[Path],
    pause_ms: list[int],
    dst: Path,
    normalize: bool = False,
) -> dict:
    """Concatenate ordered takes with inserted pauses after each but the last."""
    if len(take_paths) != len(pause_ms):
        raise ApiError(500, "internal", "Mix inputs are inconsistent")
    pieces: list[np.ndarray] = []
    sr = TARGET_SR
    per_file: list[dict] = []
    for index, path in enumerate(take_paths):
        audio, file_sr = load_audio(path)
        if file_sr != TARGET_SR:
            raise ApiError(
                500,
                "unsupported_sample_rate",
                f"Take sample rate {file_sr} is not the native 24000 Hz",
            )
        pieces.append(audio)
        per_file.append({"path": str(path), "duration": round(duration_seconds(audio, sr), 3)})
        if index < len(take_paths) - 1 and pause_ms[index] > 0:
            pieces.append(silence(pause_ms[index] / 1000.0, sr))
    mixed = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    normalized = False
    if normalize and peak > 0.0:
        mixed = mixed * (0.891 / peak)  # -1 dBFS peak, explicitly not EBU loudness
        normalized = True
    write_wav(dst, mixed, sr)
    return {
        "duration": round(duration_seconds(mixed, sr), 3),
        "sample_rate": sr,
        "normalized": normalized,
        "peak_before_normalize": round(peak, 6),
        "pieces": per_file,
        "sha256": sha256_file(dst),
    }


def temp_path(suffix: str = ".wav") -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.close()
    return Path(handle.name)
