"""Stitched WAV/ZIP export: duration, pauses, manifest and stale gates."""

from __future__ import annotations

import io
import json
import re
import zipfile

import soundfile as sf

from tests.conftest import upload_voice, wait_job


def _build_project(client, *, pauses=(300, 500, 0)):
    voice = upload_voice(client)
    project = client.post("/api/projects", json={"title": "Mix"}).json()
    segments = []
    for index, pause in enumerate(pauses, start=1):
        segments.append(
            {
                "id": f"s{index}",
                "text": f"Sentence {index}.",
                "voice_id": voice["id"],
                "mode": "clone",
                "model_id": "qwen-base-1.7b",
                "language": "ru",
                "instruction": "",
                "rate": 1.0,
                "pause_ms": pause,
                "seed": 42,
            }
        )
    saved = client.put(
        f"/api/projects/{project['id']}", json={"title": "Mix", "segments": segments}
    )
    assert saved.status_code == 200, saved.text
    job = client.post(f"/api/projects/{project['id']}/generate", json={}).json()
    assert wait_job(client, job["id"])["state"] == "done"
    return saved.json()


def test_wav_export_duration_includes_pauses(client):
    project = _build_project(client)
    job = client.post(
        f"/api/projects/{project['id']}/export", json={"format": "wav", "normalize": False}
    ).json()
    done = wait_job(client, job["id"])
    assert done["state"] == "done", done
    result = done["result"]
    assert result["format"] == "wav"
    assert result["filename"].endswith(".wav")
    download = client.get(f"/api/exports/{result['export_id']}/download")
    assert download.status_code == 200
    audio, sample_rate = sf.read(io.BytesIO(download.content))
    assert sample_rate == 24000
    duration = len(audio) / sample_rate
    # 3 x 0.25s takes + 0.30s + 0.50s inserted pauses.
    assert 1.50 <= duration <= 1.62


def test_normalized_export_reaches_minus_one_dbfs(client):
    project = _build_project(client)
    job = client.post(
        f"/api/projects/{project['id']}/export", json={"format": "wav", "normalize": True}
    ).json()
    done = wait_job(client, job["id"])
    result = done["result"]
    download = client.get(f"/api/exports/{result['export_id']}/download")
    audio, _sr = sf.read(io.BytesIO(download.content))
    peak = float(abs(audio).max())
    assert 0.85 <= peak <= 0.95


def test_zip_export_manifest(client):
    project = _build_project(client)
    job = client.post(
        f"/api/projects/{project['id']}/export", json={"format": "zip"}
    ).json()
    done = wait_job(client, job["id"])
    download = client.get(f"/api/exports/{done['result']['export_id']}/download")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "script.txt", "mixed.wav"} <= names
        assert sum(1 for name in names if name.startswith("segments/")) == 3
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["project"]["title"] == "Mix"
        assert manifest["sample_rate"] == 24000
        assert manifest["normalize"] is False
        assert len(manifest["segments"]) == 3
        entry = manifest["segments"][0]
        assert entry["take"]["id"]
        assert "path" not in entry["take"]
        assert entry["rendered"]["text"] == "Sentence 1."
        assert entry["rendered"]["model_revision"] == "fd4b254389122332181a7c3db7f27e918eec64e3"
        assert entry["rendered"]["voice"]["name"] == "Narrator"
        assert entry["voice"]["consent_basis"] == "own"
        assert entry["file"] == "segments/001-s1.wav"
        assert manifest["outputs"]["mixed"]["sha256"]
        raw_manifest = archive.read("manifest.json").decode("utf-8")
        assert re.search(r"[A-Za-z]:[\\/]", raw_manifest) is None
        script = archive.read("script.txt").decode("utf-8")
        assert "Sentence 1." in script


def test_stale_export_preserves_rendered_provenance_and_hides_paths(client):
    project = _build_project(client)
    other_voice = upload_voice(client, name="Other", transcript="Другой текст.")
    stale = dict(project["segments"][0])
    stale["text"] = "Changed sentence."
    stale["voice_id"] = other_voice["id"]
    response = client.put(
        f"/api/projects/{project['id']}",
        json={"title": "Mix", "segments": [stale, project["segments"][1], project["segments"][2]]},
    )
    assert response.status_code == 200
    refreshed = response.json()
    assert refreshed["segments"][0]["takes"][0]["stale"] is True

    blocked = client.post(
        f"/api/projects/{project['id']}/export", json={"format": "wav"}
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "stale_take"

    allowed = client.post(
        f"/api/projects/{project['id']}/export",
        json={"format": "zip", "allow_stale": True},
    ).json()
    done = wait_job(client, allowed["id"])
    assert done["state"] == "done"
    download = client.get(f"/api/exports/{done['result']['export_id']}/download")
    assert download.status_code == 200

    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        entry = manifest["segments"][0]
        # What the listener actually hears (old take), not the edited segment.
        assert entry["text"] == "Sentence 1."
        assert entry["rendered"]["text"] == "Sentence 1."
        assert entry["rendered"]["seed"] == 42
        assert entry["rendered"]["voice"]["name"] == "Narrator"
        assert entry["rendered"]["voice"]["consent_basis"] == "own"
        assert entry["rendered"]["model_revision"] == (
            "fd4b254389122332181a7c3db7f27e918eec64e3"
        )
        # Current editor state, explicitly separated.
        assert entry["requested"]["text"] == "Changed sentence."
        assert entry["requested"]["voice"]["name"] == "Other"
        assert "path" not in entry["take"]
        raw_manifest = archive.read("manifest.json").decode("utf-8")
        assert re.search(r"[A-Za-z]:[\\/]", raw_manifest) is None
        script = archive.read("script.txt").decode("utf-8")
        assert "Sentence 1." in script
        assert "requested but not narrated: Changed sentence." in script
