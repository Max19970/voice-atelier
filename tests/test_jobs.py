"""Serial queue, generation, cancellation and restart behaviour."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from studio.app import create_app
from tests.conftest import (
    TOKEN,
    create_project_with_segment,
    make_client,
    upload_voice,
    wait_job,
)
from tests.fake_engine import FakeEngine


def _reset_fake_counters() -> None:
    FakeEngine.active = 0
    FakeEngine.max_active = 0
    FakeEngine.load_count = 0


def _generate(client, project_id):
    response = client.post(f"/api/projects/{project_id}/generate", json={})
    assert response.status_code == 200, response.text
    return wait_job(client, response.json()["id"])


def test_generate_creates_and_selects_take(client):
    _reset_fake_counters()
    voice = upload_voice(client)
    project = create_project_with_segment(client, voice["id"])
    job = _generate(client, project["id"])
    assert job["state"] == "done", job
    assert len(job["result"]["generated"]) == 1
    refreshed = client.get(f"/api/projects/{project['id']}").json()
    segment = refreshed["segments"][0]
    assert segment["selected_take_id"] == segment["takes"][0]["id"]
    assert segment["takes"][0]["stale"] is False
    assert segment["takes"][0]["warnings"] == []
    audio = client.get(f"/api/takes/{segment['takes'][0]['id']}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")


def test_generate_skips_fresh_and_force_adds_take(client):
    voice = upload_voice(client)
    project = create_project_with_segment(client, voice["id"])
    _generate(client, project["id"])
    second = _generate(client, project["id"])
    assert second["result"]["skipped"] == [project["segments"][0]["id"]]
    assert second["result"]["generated"] == []
    forced = client.post(f"/api/projects/{project['id']}/generate", json={"force": True})
    forced_job = wait_job(client, forced.json()["id"])
    assert len(forced_job["result"]["generated"]) == 1
    refreshed = client.get(f"/api/projects/{project['id']}").json()
    assert len(refreshed["segments"][0]["takes"]) == 2


def test_worker_is_serial(client):
    _reset_fake_counters()
    voice = upload_voice(client)
    first = create_project_with_segment(client, voice["id"], title="One", segment_id="s-1")
    second = create_project_with_segment(client, voice["id"], title="Two", segment_id="s-2")
    job_a = client.post(f"/api/projects/{first['id']}/generate", json={}).json()
    job_b = client.post(f"/api/projects/{second['id']}/generate", json={}).json()
    assert wait_job(client, job_a["id"])["state"] == "done"
    assert wait_job(client, job_b["id"])["state"] == "done"
    assert FakeEngine.max_active == 1


def test_cancel_queued_job(tmp_path):
    _reset_fake_counters()

    def slow_factory(settings):
        return FakeEngine(settings, delay=0.5)

    client, _settings = make_client(tmp_path, factory=slow_factory)
    with client:
        voice = upload_voice(client)
        first = create_project_with_segment(client, voice["id"], title="One", segment_id="s-1")
        second = create_project_with_segment(client, voice["id"], title="Two", segment_id="s-2")
        running = client.post(f"/api/projects/{first['id']}/generate", json={}).json()
        queued = client.post(f"/api/projects/{second['id']}/generate", json={}).json()
        canceled = client.post(f"/api/jobs/{queued['id']}/cancel").json()
        assert canceled["state"] == "canceled"
        result = wait_job(client, queued["id"])
        assert result["state"] == "canceled"
        assert wait_job(client, running["id"])["state"] == "done"


def test_cancel_running_job(tmp_path):
    _reset_fake_counters()

    def slow_factory(settings):
        return FakeEngine(settings, delay=0.5)

    client, _settings = make_client(tmp_path, factory=slow_factory)
    with client:
        voice = upload_voice(client)
        project = create_project_with_segment(client, voice["id"])
        job = client.post(f"/api/projects/{project['id']}/generate", json={}).json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = client.get(f"/api/jobs/{job['id']}").json()["state"]
            if state == "running":
                break
            time.sleep(0.02)
        client.post(f"/api/jobs/{job['id']}/cancel")
        result = wait_job(client, job["id"])
        assert result["state"] == "canceled"
        refreshed = client.get(f"/api/projects/{project['id']}").json()
        assert refreshed["segments"][0]["selected_take_id"] is None


def test_missing_model_blocks_generation(tmp_path):
    client, _settings = make_client(tmp_path, install_models=False)
    with client:
        response = client.post("/api/projects/x/generate", json={})
        assert response.status_code == 404  # unknown project first

        project = client.post("/api/projects", json={"title": "No model"}).json()
        segment = {
            "id": "s-1",
            "text": "a",
            "mode": "design",
            "model_id": "qwen-design-1.7b",
            "instruction": "calm",
            "language": "ru",
        }
        client.put(f"/api/projects/{project['id']}", json={"title": "No model", "segments": [segment]})
        response = client.post(f"/api/projects/{project['id']}/generate", json={})
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "model_not_installed"


def test_missing_transcript_blocks_generate(client):
    voice = upload_voice(client, transcript="")
    project = create_project_with_segment(client, voice["id"])
    response = client.post(f"/api/projects/{project['id']}/generate", json={})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "missing_transcript"


def test_export_requires_selected_take(client):
    voice = upload_voice(client)
    project = create_project_with_segment(client, voice["id"])
    response = client.post(f"/api/projects/{project['id']}/export", json={"format": "wav"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "missing_take"


def test_job_history_survives_restart(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        voice = upload_voice(client)
        project = create_project_with_segment(client, voice["id"])
        job = _generate(client, project["id"])
        assert job["state"] == "done"

    app = create_app(settings, engine_factory=lambda s: FakeEngine(s), web_dir=tmp_path / "missing-web")
    with TestClient(app, base_url="http://127.0.0.1:8765") as restarted:
        restarted.headers.update({"X-Studio-Token": TOKEN})
        jobs = restarted.get("/api/jobs").json()["items"]
        assert any(item["id"] == job["id"] and item["state"] == "done" for item in jobs)
