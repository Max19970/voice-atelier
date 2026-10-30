"""Project persistence, client ids, validation and title/segment preservation."""

from __future__ import annotations

from tests.conftest import create_project_with_segment, upload_voice


def test_project_crud(client):
    created = client.post("/api/projects", json={"title": "Новый проект"})
    assert created.status_code == 201
    project = created.json()
    assert project["segments"] == []
    assert client.get(f"/api/projects/{project['id']}").status_code == 200
    listing = client.get("/api/projects").json()["items"]
    assert any(item["id"] == project["id"] for item in listing)
    assert client.delete(f"/api/projects/{project['id']}").json() == {"ok": True}
    assert client.get(f"/api/projects/{project['id']}").status_code == 404


def test_client_supplied_segment_ids_are_kept(client):
    voice = upload_voice(client)
    project = create_project_with_segment(client, voice["id"], segment_id="0f0f0f0f-aaaa-4bbb-8ccc-123456789abc")
    assert project["segments"][0]["id"] == "0f0f0f0f-aaaa-4bbb-8ccc-123456789abc"


def test_duplicate_segment_ids_rejected(client):
    voice = upload_voice(client)
    project = client.post("/api/projects", json={"title": "Dup"}).json()
    segment = {
        "id": "dup-1",
        "text": "a",
        "voice_id": voice["id"],
        "mode": "clone",
        "model_id": "qwen-base-1.7b",
        "language": "ru",
    }
    response = client.put(
        f"/api/projects/{project['id']}",
        json={"title": "Dup", "segments": [segment, dict(segment)]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "duplicate_segment_id"


def test_segment_id_from_other_project_rejected(client):
    voice = upload_voice(client)
    create_project_with_segment(client, voice["id"], title="A", segment_id="shared-id")
    other = client.post("/api/projects", json={"title": "B"}).json()
    segment = {
        "id": "shared-id",
        "text": "a",
        "voice_id": voice["id"],
        "mode": "clone",
        "model_id": "qwen-base-1.7b",
        "language": "ru",
    }
    response = client.put(f"/api/projects/{other['id']}", json={"title": "B", "segments": [segment]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_segment_id"


def test_clone_instruction_rejected(client):
    voice = upload_voice(client)
    project = client.post("/api/projects", json={"title": "Instr"}).json()
    response = client.put(
        f"/api/projects/{project['id']}",
        json={
            "title": "Instr",
            "segments": [
                {
                    "text": "a",
                    "voice_id": voice["id"],
                    "mode": "clone",
                    "model_id": "qwen-base-1.7b",
                    "language": "ru",
                    "instruction": "angry",
                }
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "clone_instruction_not_supported"


def test_invalid_model_and_mode_rejected(client):
    voice = upload_voice(client)
    project = client.post("/api/projects", json={"title": "M"}).json()
    base = {"text": "a", "voice_id": voice["id"], "language": "ru"}
    bad_model = client.put(
        f"/api/projects/{project['id']}",
        json={"segments": [{**base, "mode": "clone", "model_id": "qwen-design-1.7b"}]},
    )
    assert bad_model.status_code == 422
    assert bad_model.json()["error"]["code"] == "invalid_model"


def test_too_many_segments_rejected(client):
    project = client.post("/api/projects", json={"title": "Many"}).json()
    segments = [
        {"id": f"s-{index}", "text": "a", "mode": "design", "model_id": "qwen-design-1.7b", "instruction": "x", "language": "ru"}
        for index in range(201)
    ]
    response = client.put(f"/api/projects/{project['id']}", json={"title": "Many", "segments": segments})
    assert response.status_code == 422


def test_save_without_title_keeps_it(client):
    project = client.post("/api/projects", json={"title": "Keep"}).json()
    updated = client.put(f"/api/projects/{project['id']}", json={"segments": []}).json()
    assert updated["title"] == "Keep"


def test_design_segment_text_capped_at_one_identity(client):
    project = client.post("/api/projects", json={"title": "Design"}).json()
    response = client.put(
        f"/api/projects/{project['id']}",
        json={
            "title": "Design",
            "segments": [
                {
                    "id": "d-1",
                    "text": "Я" * 241,
                    "mode": "design",
                    "model_id": "qwen-design-1.7b",
                    "instruction": "calm",
                    "language": "ru",
                }
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "design_text_too_long"
