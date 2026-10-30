"""Model catalog pins, offline rules, license gate and trusted-digest verification."""

from __future__ import annotations

import hashlib
import json

import pytest

from studio.engine.catalog import MODELS, MODELS_BY_ID, installed_state, model_dir
from studio.engine.downloader import ExpectedFile, _git_blob_sha1, verify_file
from studio.errors import EngineError
from tests.conftest import make_client


def test_models_are_pinned_and_installed(client):
    items = client.get("/api/models").json()["items"]
    assert len(items) == 3
    by_id = {item["id"]: item for item in items}
    assert by_id["qwen-base-1.7b"]["revision"] == "fd4b254389122332181a7c3db7f27e918eec64e3"
    assert by_id["qwen-base-0.6b"]["revision"] == "5d83992436eae1d760afd27aff78a71d676296fc"
    assert by_id["qwen-design-1.7b"]["revision"] == "5ecdb67327fd37bb2e042aab12ff7391903235d3"
    assert all(item["installed"] for item in items)
    assert all(item["license"] == "Apache-2.0" for item in items)


def test_download_requires_license_acceptance(client):
    response = client.post("/api/models/qwen-base-1.7b/download", json={"accept_license": False})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "license_required"


def test_offline_blocks_download(tmp_path):
    client, _settings = make_client(tmp_path, offline=True)
    with client:
        response = client.post(
            "/api/models/qwen-base-1.7b/download", json={"accept_license": True}
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "offline"


def test_verify_lfs_sha256(tmp_path):
    path = tmp_path / "model.safetensors"
    data = b"weights" * 10
    path.write_bytes(data)
    good = ExpectedFile("model.safetensors", len(data), hashlib.sha256(data).hexdigest(), None)
    assert verify_file(path, good) == "lfs-sha256"
    bad = ExpectedFile("model.safetensors", len(data), "0" * 64, None)
    with pytest.raises(EngineError) as exc:
        verify_file(path, bad)
    assert exc.value.code == "integrity_mismatch"


def test_verify_git_blob_fallback(tmp_path):
    path = tmp_path / "tokenizer_config.json"
    path.write_bytes(b'{"a": 1}')
    expected = ExpectedFile("tokenizer_config.json", path.stat().st_size, None, _git_blob_sha1(path))
    assert verify_file(path, expected) == "git-blob-sha1"


def test_unverifiable_file_fails_closed(tmp_path):
    path = tmp_path / "x.json"
    path.write_bytes(b"{}")
    with pytest.raises(EngineError) as exc:
        verify_file(path, ExpectedFile("x.json", path.stat().st_size, None, None))
    assert exc.value.code == "unverified_download"


def test_partial_install_not_reported_as_installed(tmp_path):
    client, settings = make_client(tmp_path)
    with client:
        spec = next(item for item in MODELS if item.id == "qwen-design-1.7b")
        (model_dir(settings, spec.id) / "model.safetensors").unlink()
        installed, status_text, missing = installed_state(settings, spec)
        assert installed is False
        assert status_text == "partial"
        assert "model.safetensors" in missing
        items = client.get("/api/models").json()["items"]
        entry = next(item for item in items if item["id"] == spec.id)
        assert entry["installed"] is False
        assert entry["status"] == "partial"


def test_status_reports_capabilities(client):
    body = client.get("/api/status").json()
    assert body["capabilities"]["clone_instruction"] is False
    assert body["capabilities"]["design_instruction"] is True
    assert body["capabilities"]["rate"] == {"min": 0.5, "max": 2.0, "mode": "postprocess"}
    assert body["capabilities"]["max_design_chars"] == 240
    assert "ru" in body["capabilities"]["languages"]
    assert body["paths"]["data"]


def test_tampered_manifest_fails_closed(tmp_path):
    client, settings = make_client(tmp_path)
    spec = MODELS_BY_ID["qwen-base-1.7b"]
    with client:
        manifest_path = model_dir(settings, spec.id) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["revision"] = "0" * 40
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        installed, status, problems = installed_state(settings, spec)
        assert installed is False
        assert status == "partial"
        assert problems == ["manifest.json"]

        entry = next(
            item for item in client.get("/api/models").json()["items"] if item["id"] == spec.id
        )
        assert entry["installed"] is False
        assert entry["status"] == "partial"


def test_missing_manifest_fails_closed(tmp_path):
    client, settings = make_client(tmp_path)
    spec = MODELS_BY_ID["qwen-design-1.7b"]
    with client:
        (model_dir(settings, spec.id) / "manifest.json").unlink()
        installed, status, problems = installed_state(settings, spec)
        assert installed is False
        assert status == "partial"
        assert problems == ["manifest.json"]
