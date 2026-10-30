"""Voice Design: new identity, consent, history and synthetic voice saving."""

from __future__ import annotations

from tests.conftest import make_client, wait_job
from tests.fake_engine import FakeEngine

DESIGN_BODY = {
    "name": "Hero",
    "text": "Hello there. This is a test.",
    "instruction": "Warm adult male voice, calm and clear",
    "language": "en",
    "seed": 7,
    "save_as_voice": False,
    "consent": True,
}


def test_design_requires_consent(client):
    body = dict(DESIGN_BODY, consent=False)
    response = client.post("/api/design", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consent_required"


def test_design_rejects_multi_chunk_text(client):
    body = dict(DESIGN_BODY, text="Я" * 241)
    response = client.post("/api/design", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "design_text_too_long"


def test_design_job_and_history(client):
    response = client.post("/api/design", json=DESIGN_BODY)
    assert response.status_code == 200, response.text
    job = wait_job(client, response.json()["id"])
    assert job["state"] == "done", job
    take_id = job["result"]["take_id"]
    assert job["result"]["voice_id"] is None

    items = client.get("/api/designs").json()["items"]
    record = next(item for item in items if item["take_id"] == take_id)
    assert record["text"] == DESIGN_BODY["text"]
    assert record["instruction"] == DESIGN_BODY["instruction"]
    assert record["seed"] == 7
    assert record["language"] == "en"
    assert client.get(f"/api/takes/{take_id}/audio").status_code == 200


def test_design_can_save_synthetic_voice(client):
    body = dict(DESIGN_BODY, name="Designed hero", save_as_voice=True)
    job = wait_job(client, client.post("/api/design", json=body).json()["id"])
    assert job["state"] == "done", job
    voice_id = job["result"]["voice_id"]
    assert voice_id
    voices = client.get("/api/voices").json()["items"]
    voice = next(item for item in voices if item["id"] == voice_id)
    assert voice["source"] == "design"
    assert voice["consent_basis"] == "synthetic"
    assert voice["transcript"] == body["text"]
    assert voice["duration"] > 0


def test_design_requires_installed_model(tmp_path):
    client, _settings = make_client(tmp_path, install_models=False)
    with client:
        response = client.post("/api/design", json=DESIGN_BODY)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "model_not_installed"


def test_too_short_design_clip_fails_cleanly_without_voice(tmp_path):
    client, settings = make_client(
        tmp_path, factory=lambda s: FakeEngine(s, design_duration=0.2)
    )
    with client:
        body = dict(DESIGN_BODY, name="Too short", save_as_voice=True)
        job = wait_job(client, client.post("/api/design", json=body).json()["id"])
        assert job["state"] == "failed", job
        assert job["error"]["code"] == "design_clip_too_short"
        assert client.get("/api/voices").json()["items"] == []
        assert client.get("/api/designs").json()["items"] == []
        assert list(settings.takes_dir.iterdir()) == []
