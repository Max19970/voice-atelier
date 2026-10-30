"""Pinned model catalog. Revisions are HF repo commit SHAs (queried 2026-10-29).

A model counts as installed only when a verified manifest for the exact pinned
model_id/repo/revision exists and every file it lists is present at the recorded
size. Files alone are never enough: a canceled download or a stale manifest from
an older revision must fail closed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings


@dataclass(frozen=True)
class ModelSpec:
    id: str
    label: str
    repo: str
    revision: str
    kind: str  # "clone" | "design"
    size_gb: float
    license: str
    description: str


MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(
        id="qwen-base-1.7b",
        label="Qwen3-TTS 1.7B Base (voice cloning)",
        repo="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
        revision="fd4b254389122332181a7c3db7f27e918eec64e3",
        kind="clone",
        size_gb=4.5,
        license="Apache-2.0",
        description="Reference-audio voice cloning (needs a reference clip and its transcript).",
    ),
    ModelSpec(
        id="qwen-base-0.6b",
        label="Qwen3-TTS 0.6B Base (voice cloning, smaller)",
        repo="Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        revision="5d83992436eae1d760afd27aff78a71d676296fc",
        kind="clone",
        size_gb=2.5,
        license="Apache-2.0",
        description="Faster/lower-VRAM cloning model with slightly lower quality than 1.7B.",
    ),
    ModelSpec(
        id="qwen-design-1.7b",
        label="Qwen3-TTS 1.7B VoiceDesign (new voice from description)",
        repo="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign",
        revision="5ecdb67327fd37bb2e042aab12ff7391903235d3",
        kind="design",
        size_gb=4.5,
        license="Apache-2.0",
        description="Creates a NEW synthetic identity from a text description; not a clone.",
    ),
)

MODELS_BY_ID: dict[str, ModelSpec] = {spec.id: spec for spec in MODELS}

CLONE_MODEL_IDS = frozenset(spec.id for spec in MODELS if spec.kind == "clone")
DESIGN_MODEL_IDS = frozenset(spec.id for spec in MODELS if spec.kind == "design")
DEFAULT_CLONE_MODEL = "qwen-base-1.7b"
DEFAULT_DESIGN_MODEL = "qwen-design-1.7b"

# Every file the qwen-tts loader needs, including the nested 12 Hz speech
# tokenizer that ships inside each model repo (no separate download, no pickle).
REQUIRED_FILES: tuple[str, ...] = (
    "config.json",
    "generation_config.json",
    "preprocessor_config.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "model.safetensors",
    "speech_tokenizer/config.json",
    "speech_tokenizer/configuration.json",
    "speech_tokenizer/preprocessor_config.json",
    "speech_tokenizer/model.safetensors",
)

ALLOWED_PATTERNS: tuple[str, ...] = ("*.json", "*.txt", "*.safetensors")


def model_dir(settings: Settings, model_id: str) -> Path:
    return settings.models_dir / model_id


def _manifest_problems(target: Path, spec: ModelSpec) -> tuple[dict | None, list[str]]:
    """Validate manifest identity, revision and every listed file size."""
    path = target / "manifest.json"
    if not path.is_file():
        return None, ["manifest.json"]
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, ["manifest.json"]
    if not isinstance(manifest, dict) or manifest.get("verified") is not True:
        return None, ["manifest.json"]
    if manifest.get("model_id") != spec.id:
        return None, ["manifest.json"]
    if manifest.get("repo") != spec.repo:
        return None, ["manifest.json"]
    if manifest.get("revision") != spec.revision:
        return None, ["manifest.json"]
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        return None, ["manifest.json"]

    listed: dict[str, int] = {}
    for entry in files:
        if not isinstance(entry, dict):
            return None, ["manifest.json"]
        name = entry.get("name")
        size = entry.get("size")
        if (
            not isinstance(name, str)
            or not name
            or name.startswith("/")
            or "\\" in name
            or ".." in name.split("/")
        ):
            return None, ["manifest.json"]
        if not isinstance(size, int) or size < 0:
            return None, ["manifest.json"]
        listed[name] = size

    problems: list[str] = [name for name in REQUIRED_FILES if name not in listed]
    for name, size in listed.items():
        candidate = target / name
        if not candidate.is_file() or candidate.stat().st_size != size:
            problems.append(name)
    if problems:
        return None, sorted(set(problems))
    return manifest, []


def installed_state(settings: Settings, spec: ModelSpec) -> tuple[bool, str, list[str]]:
    """Return (installed, status, problems). Status: installed|partial|missing.

    ``installed`` requires a verified manifest matching the pinned
    model_id/repo/revision with all listed files present at expected sizes.
    """
    target = model_dir(settings, spec.id)
    if not target.is_dir():
        return False, "missing", list(REQUIRED_FILES)
    manifest, problems = _manifest_problems(target, spec)
    if manifest is None:
        return False, "partial", problems
    return True, "installed", []
