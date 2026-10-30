"""Localhost security: token, Host/Origin allowlists, CSP, bounded JSON bodies."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import TOKEN


def test_health_needs_no_token(tmp_client):
    client, _settings, _tmp = tmp_client
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_requires_token(tmp_client):
    client, _settings, _tmp = tmp_client
    assert client.get("/api/status", headers={"X-Studio-Token": ""}).status_code == 401
    assert client.get("/api/status", headers={"X-Studio-Token": "wrong"}).status_code == 401
    assert client.get("/api/status").status_code == 200


def test_foreign_host_is_rejected(tmp_client):
    client, _settings, _tmp = tmp_client
    hostile = TestClient(client.app, base_url="http://evil.example:8765")
    response = hostile.get("/api/status", headers={"X-Studio-Token": TOKEN})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_host"


def test_foreign_origin_on_mutation_is_rejected(tmp_client):
    client, _settings, _tmp = tmp_client
    response = client.post(
        "/api/projects",
        json={"title": "x"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_origin"


def test_index_injects_token_and_is_not_cached(tmp_client):
    client, settings, _tmp = tmp_client
    response = client.get("/")
    assert response.status_code == 200
    assert settings.token in response.text
    assert "__STUDIO_TOKEN__" not in response.text
    assert "no-store" in response.headers.get("cache-control", "")
    assert "script-src 'self'" in response.headers.get("content-security-policy", "")


def test_oversized_json_body_is_rejected(tmp_client):
    client, _settings, _tmp = tmp_client
    response = client.post("/api/projects", json={"title": "x" * (4 * 1024 * 1024 + 10)})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
