"""Downloader robustness: verified local reuse + bounded transient retries.

No network here: HF metadata/download functions are injected/monkeypatched.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from studio.engine import downloader
from studio.engine.catalog import MODELS_BY_ID, REQUIRED_FILES, installed_state, model_dir
from studio.engine.downloader import ExpectedFile, _git_blob_sha1, verify_file
from studio.errors import EngineCancelled, EngineError
from tests.conftest import build_settings

CODEC = "speech_tokenizer/model.safetensors"


def _safetensors_bytes(payload: bytes = b"abcd") -> bytes:
    header = json.dumps(
        {"w": {"dtype": "F32", "shape": [1], "data_offsets": [0, len(payload)]}}
    ).encode("utf-8")
    return len(header).to_bytes(8, "little") + header + payload


def _install_model(settings, spec, contents: dict[str, bytes]) -> None:
    """Create a full, verified test install (required files + manifest)."""
    target = model_dir(settings, spec.id)
    entries = []
    for name in REQUIRED_FILES:
        data = contents.get(name)
        if data is None:
            if name.endswith(".safetensors"):
                data = _safetensors_bytes()
            elif name.endswith(".json"):
                data = b"{}"
            else:
                data = b""
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries.append(
            {
                "name": name,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "expected_sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "model_id": spec.id,
        "repo": spec.repo,
        "revision": spec.revision,
        "verified": True,
        "files": entries,
    }
    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_reuse_copies_matching_verified_file_without_network(tmp_path):
    settings = build_settings(tmp_path)
    source_spec = MODELS_BY_ID["qwen-base-1.7b"]
    target_spec = MODELS_BY_ID["qwen-design-1.7b"]
    data = _safetensors_bytes(b"shared-codec")
    _install_model(settings, source_spec, {CODEC: data})

    target_dir = model_dir(settings, target_spec.id)
    target_dir.mkdir(parents=True)
    expected = ExpectedFile(CODEC, len(data), hashlib.sha256(data).hexdigest(), None)

    reused = downloader.try_reuse_local_file(settings, target_spec, target_dir, CODEC, expected)
    assert reused == source_spec.id
    assert (target_dir / CODEC).read_bytes() == data
    assert verify_file(target_dir / CODEC, expected) == "lfs-sha256"


def test_reuse_rejects_wrong_expected_digest(tmp_path):
    settings = build_settings(tmp_path)
    source_spec = MODELS_BY_ID["qwen-base-1.7b"]
    target_spec = MODELS_BY_ID["qwen-design-1.7b"]
    data = _safetensors_bytes(b"shared-codec")
    _install_model(settings, source_spec, {CODEC: data})
    target_dir = model_dir(settings, target_spec.id)
    target_dir.mkdir(parents=True)

    expected = ExpectedFile(CODEC, len(data), "0" * 64, None)
    assert downloader.try_reuse_local_file(settings, target_spec, target_dir, CODEC, expected) is None
    assert not (target_dir / CODEC).exists()


def test_reuse_rechecks_source_actual_hash(tmp_path):
    settings = build_settings(tmp_path)
    source_spec = MODELS_BY_ID["qwen-base-1.7b"]
    target_spec = MODELS_BY_ID["qwen-design-1.7b"]
    data = _safetensors_bytes(b"shared-codec")
    _install_model(settings, source_spec, {CODEC: data})
    # Same size, different bytes: the manifest still claims the old digest.
    tampered = b"Z" * len(data)
    (model_dir(settings, source_spec.id) / CODEC).write_bytes(tampered)

    target_dir = model_dir(settings, target_spec.id)
    target_dir.mkdir(parents=True)
    expected = ExpectedFile(CODEC, len(data), hashlib.sha256(data).hexdigest(), None)
    assert downloader.try_reuse_local_file(settings, target_spec, target_dir, CODEC, expected) is None
    assert not (target_dir / CODEC).exists()


def test_reuse_matches_git_blob_id(tmp_path):
    settings = build_settings(tmp_path)
    source_spec = MODELS_BY_ID["qwen-base-1.7b"]
    target_spec = MODELS_BY_ID["qwen-design-1.7b"]
    name = "tokenizer_config.json"
    data = b'{"a": 1}'
    _install_model(settings, source_spec, {name: data})
    source_file = model_dir(settings, source_spec.id) / name
    blob_id = _git_blob_sha1(source_file)

    manifest_path = model_dir(settings, source_spec.id) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["name"] == name:
            entry["expected_sha256"] = None
            entry["expected_blob_id"] = blob_id
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    target_dir = model_dir(settings, target_spec.id)
    target_dir.mkdir(parents=True)
    expected = ExpectedFile(name, len(data), None, blob_id)
    assert downloader.try_reuse_local_file(settings, target_spec, target_dir, name, expected) == source_spec.id
    assert (target_dir / name).read_bytes() == data


def test_retry_transient_then_succeeds(monkeypatch, tmp_path):
    settings = build_settings(tmp_path)
    spec = MODELS_BY_ID["qwen-base-0.6b"]
    calls: list[str] = []

    def fake(**kwargs):
        calls.append(kwargs["filename"])
        if len(calls) < 3:
            raise RuntimeError(
                "IncompleteRead(31663976 bytes read, 650629116 more expected)"
            )
        return "ok"

    monkeypatch.setattr(downloader, "_hf_download", fake)
    downloader._download_file_with_retry(
        "model.safetensors", spec, model_dir(settings, spec.id), lambda: False, delays=(0.001, 0.001)
    )
    assert len(calls) == 3


def test_retry_permanent_error_fails_fast(monkeypatch, tmp_path):
    settings = build_settings(tmp_path)
    spec = MODELS_BY_ID["qwen-base-0.6b"]
    calls: list[str] = []

    def fake(**kwargs):
        calls.append(kwargs["filename"])
        raise RuntimeError("404 Client Error: Not Found for url https://huggingface.co/x")

    monkeypatch.setattr(downloader, "_hf_download", fake)
    with pytest.raises(EngineError) as excinfo:
        downloader._download_file_with_retry(
            "model.safetensors", spec, model_dir(settings, spec.id), lambda: False, delays=(0.001,)
        )
    assert excinfo.value.code == "download_failed"
    assert len(calls) == 1


def test_retry_can_be_cancelled_during_backoff(monkeypatch, tmp_path):
    settings = build_settings(tmp_path)
    spec = MODELS_BY_ID["qwen-base-0.6b"]
    calls: list[str] = []

    def fake(**kwargs):
        calls.append(kwargs["filename"])
        raise RuntimeError("Connection reset by peer")

    monkeypatch.setattr(downloader, "_hf_download", fake)
    with pytest.raises(EngineCancelled):
        downloader._download_file_with_retry(
            "model.safetensors",
            spec,
            model_dir(settings, spec.id),
            lambda: len(calls) >= 1,
            delays=(5.0,),
        )
    assert len(calls) == 1


def test_download_model_reuses_codec_and_skips_its_network_download(monkeypatch, tmp_path):
    settings = build_settings(tmp_path)
    source_spec = MODELS_BY_ID["qwen-base-1.7b"]
    target_spec = MODELS_BY_ID["qwen-design-1.7b"]
    codec_data = _safetensors_bytes(b"shared-codec")
    _install_model(settings, source_spec, {CODEC: codec_data})

    contents: dict[str, bytes] = {}
    for name in REQUIRED_FILES:
        if name == CODEC:
            contents[name] = codec_data
        elif name.endswith(".safetensors"):
            # Distinct from the source model so only the codec can be reused.
            contents[name] = _safetensors_bytes(b"target-only")
        elif name.endswith(".json"):
            contents[name] = b'{"target": 1}'
        else:
            contents[name] = b"target-only"
    expected = {
        name: ExpectedFile(name, len(data), hashlib.sha256(data).hexdigest(), None)
        for name, data in contents.items()
    }

    monkeypatch.setattr(downloader, "_hf_api", lambda: object())
    monkeypatch.setattr(downloader, "_metadata_from_model_info", lambda api, spec: expected)
    downloaded: list[str] = []

    def fake_download(**kwargs):
        name = kwargs["filename"]
        downloaded.append(name)
        path = model_dir(settings, target_spec.id) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents[name])
        return str(path)

    monkeypatch.setattr(downloader, "_hf_download", fake_download)

    manifest = downloader.download_model(settings, target_spec, lambda value, stage: None, lambda: False)

    assert CODEC not in downloaded
    codec_entry = next(entry for entry in manifest["files"] if entry["name"] == CODEC)
    assert codec_entry["reused_from"] == source_spec.id
    assert manifest["verified"] is True
    installed, status, problems = installed_state(settings, target_spec)
    assert installed is True, (status, problems)
