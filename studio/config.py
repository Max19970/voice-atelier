"""Runtime configuration for Voice Atelier."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_LANGUAGES: tuple[str, ...] = ("ru", "en", "de", "es", "fr", "it", "ja", "ko", "pt", "zh")

# Engine language names accepted by the Qwen3-TTS checkpoints.
LANGUAGE_NAMES: dict[str, str] = {
    "ru": "Russian",
    "en": "English",
    "de": "German",
    "es": "Spanish",
    "fr": "French",
    "it": "Italian",
    "ja": "Japanese",
    "ko": "Korean",
    "pt": "Portuguese",
    "zh": "Chinese",
}

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# VoiceDesign generates a new identity per call; >240 chars would silently
# change identity between chunks, so long design text is rejected and users are
# told to save the designed voice and clone longer text with the Base model.
DESIGN_MAX_CHARS = 240

_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """All runtime paths and limits. Construct via :meth:`from_env` in production."""

    home: Path
    host: str = "127.0.0.1"
    port: int = 8765
    offline: bool = False
    device: str = "auto"
    web_dir: Path = field(default_factory=lambda: _DEFAULT_REPO_ROOT / "web")
    token: str = ""

    # Limits from the frozen contract.
    max_upload_bytes: int = 50 * 1024 * 1024
    max_json_bytes: int = 4 * 1024 * 1024
    max_active_jobs: int = 32
    max_ref_seconds: float = 60.0
    min_ref_seconds: float = 2.0
    max_selected_seconds: float = 30.0
    max_segments: int = 200
    max_text_chars: int = 10000
    max_instruction_chars: int = 1500
    chunk_chars: int = 240
    subprocess_timeout: int = 240
    job_history_limit: int = 100

    def __post_init__(self) -> None:
        self.home = Path(self.home)
        self.web_dir = Path(self.web_dir)
        if not self.token:
            self.token = secrets.token_urlsafe(32)

    # ---- derived paths -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.home

    @property
    def db_path(self) -> Path:
        return self.home / "voice_atelier.sqlite3"

    @property
    def voices_dir(self) -> Path:
        return self.home / "voices"

    @property
    def takes_dir(self) -> Path:
        return self.home / "takes"

    @property
    def exports_dir(self) -> Path:
        return self.home / "exports"

    @property
    def models_dir(self) -> Path:
        return self.home / "models"

    @property
    def tmp_dir(self) -> Path:
        return self.home / "tmp"

    # ---- security ------------------------------------------------------
    @property
    def allowed_hosts(self) -> set[str]:
        return {
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        }

    @property
    def allowed_origins(self) -> set[str]:
        return {f"http://{host}" for host in self.allowed_hosts}

    # ---- lifecycle -----------------------------------------------------
    def ensure_dirs(self) -> None:
        for path in (
            self.home,
            self.voices_dir,
            self.takes_dir,
            self.exports_dir,
            self.models_dir,
            self.tmp_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, home: Path | None = None) -> "Settings":
        env_home = os.environ.get("VOICE_ATELIER_HOME")
        resolved_home = Path(env_home) if env_home else (home or _DEFAULT_REPO_ROOT / "data")
        device = os.environ.get("VOICE_ATELIER_DEVICE", "auto").strip().lower()
        if device not in {"auto", "cuda", "cpu"}:
            device = "auto"
        try:
            port = int(os.environ.get("VOICE_ATELIER_PORT", "8765"))
        except ValueError:
            port = 8765
        host = os.environ.get("VOICE_ATELIER_HOST", "127.0.0.1").strip() or "127.0.0.1"
        return cls(
            home=resolved_home,
            host=host,
            port=port,
            offline=_env_flag("VOICE_ATELIER_OFFLINE", False),
            device=device,
        )
