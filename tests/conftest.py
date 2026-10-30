"""Shared fixtures: isolated data dir, injected deterministic adapter, token client."""

from __future__ import annotations

import io
import hashlib
import json
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from studio.app import create_app
from studio.config import Settings
from studio.engine.catalog import MODELS, REQUIRED_FILES, model_dir
from tests.fake_engine import FakeEngine

TOKEN = "test-token-0123456789"


def make_wav_bytes(seconds: float = 3.0, sr: int = 24000, freq: float = 220.0) -> bytes:
    t = np.arange(int(seconds * sr)) / sr
    audio = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    buffer = io.BytesIO()
    sf.write(buffer, audio, sr, format="WAV", subtype="PCM_16")
    return buffer.getvalue()


def install_dummy_models(settings: Settings) -> None:
    """Write required files plus a manifest that satisfies the verified-install contract."""
    for spec in MODELS:
        target = model_dir(settings, spec.id)
        target.mkdir(parents=True, exist_ok=True)
        entries = []
        for name in REQUIRED_FILES:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(b"{}" if name.endswith(".json") else b"")
            entries.append(
                {
                    "name": name,
                    "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "verified_by": "test",
                }
            )
        manifest = {
            "model_id": spec.id,
            "repo": spec.repo,
            "revision": spec.revision,
            "license": spec.license,
            "verified": True,
            "downloaded_at": "2026-01-01T00:00:00Z",
            "files": entries,
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def build_settings(tmp_path: Path, *, offline: bool = False) -> Settings:
    settings = Settings(
        home=tmp_path / "data",
        port=8765,
        token=TOKEN,
        device="cpu",
        offline=offline,
    )
    settings.ensure_dirs()
    return settings


def make_client(
    tmp_path: Path, *, factory=None, offline: bool = False, install_models: bool = True
) -> tuple[TestClient, Settings]:
    settings = build_settings(tmp_path, offline=offline)
    if install_models:
        install_dummy_models(settings)
    app = create_app(
        settings,
        engine_factory=factory or (lambda s: FakeEngine(s)),
        web_dir=tmp_path / "missing-web",
    )

    class _Client(TestClient):
        pass

    client = _Client(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False)
    client.headers.update({"X-Studio-Token": TOKEN})
    return client, settings


@pytest.fixture
def tmp_client(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        yield client, settings, tmp_path


@pytest.fixture
def client(tmp_client):
    return tmp_client[0]


@pytest.fixture
def settings(tmp_client):
    return tmp_client[1]


def wait_job(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] in {"done", "failed", "canceled"}:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish in time")


def upload_voice(client: TestClient, *, seconds: float = 3.0, **overrides) -> dict:
    data = {
        "name": "Narrator",
        "language": "ru",
        "transcript": "Привет, это тестовая фраза.",
        "consent": "true",
        "consent_basis": "own",
        "trim_start": "0",
    }
    data.update(overrides)
    response = client.post(
        "/api/voices",
        data=data,
        files={"file": ("ref.wav", make_wav_bytes(seconds), "audio/wav")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def create_project_with_segment(
    client: TestClient,
    voice_id: str,
    *,
    title: str = "Demo",
    segment_id: str = "seg-0001",
    text: str = "Привет. Это тест.",
    pause_ms: int = 300,
) -> dict:
    project = client.post("/api/projects", json={"title": title}).json()
    segment = {
        "id": segment_id,
        "text": text,
        "voice_id": voice_id,
        "mode": "clone",
        "model_id": "qwen-base-1.7b",
        "language": "ru",
        "instruction": "",
        "rate": 1.0,
        "pause_ms": pause_ms,
        "seed": 42,
    }
    response = client.put(f"/api/projects/{project['id']}", json={"title": title, "segments": [segment]})
    assert response.status_code == 200, response.text
    return response.json()
