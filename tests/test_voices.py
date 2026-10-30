"""Voice library: consent, trim, quality, reference gates and deletion rules."""

from __future__ import annotations

from tests.conftest import (
    create_project_with_segment,
    make_wav_bytes,
    upload_voice,
    wait_job,
)


def test_voice_upload_and_quality(client):
    voice = upload_voice(client)
    assert voice["source"] == "upload"
    assert voice["consent_basis"] == "own"
    assert voice["sample_rate"] == 24000
    for key in ("peak_dbfs", "rms_dbfs", "clipped_fraction", "silence_fraction", "warnings"):
        assert key in voice["quality"]
    audio = client.get(f"/api/voices/{voice['id']}/audio")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")


def test_consent_required(client):
    response = client.post(
        "/api/voices",
        data={"name": "X", "language": "ru", "transcript": "t", "consent": "false", "consent_basis": "own"},
        files={"file": ("ref.wav", make_wav_bytes(3), "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "consent_required"


def test_invalid_consent_basis(client):
    response = client.post(
        "/api/voices",
        data={"name": "X", "language": "ru", "transcript": "t", "consent": "true", "consent_basis": "because"},
        files={"file": ("ref.wav", make_wav_bytes(3), "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_consent_basis"


def test_short_reference_rejected(client):
    response = client.post(
        "/api/voices",
        data={"name": "X", "language": "ru", "transcript": "t", "consent": "true", "consent_basis": "own"},
        files={"file": ("ref.wav", make_wav_bytes(1.0), "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reference_too_short"


def test_long_reference_rejected(client):
    response = client.post(
        "/api/voices",
        data={"name": "X", "language": "ru", "transcript": "t", "consent": "true", "consent_basis": "own"},
        files={"file": ("ref.wav", make_wav_bytes(61), "audio/wav")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "reference_too_long"


def test_trim_is_applied(client):
    voice = upload_voice(client, seconds=6.0, trim_start="1", trim_end="4")
    assert 2.9 <= voice["duration"] <= 3.1


def test_playlist_is_rejected(client):
    response = client.post(
        "/api/voices",
        data={"name": "X", "language": "ru", "transcript": "t", "consent": "true", "consent_basis": "own"},
        files={"file": ("list.m3u", b"#EXTM3U\nhttp://evil.example/x.mp3\n", "audio/mpegurl")},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_audio"


def test_voice_in_use_cannot_be_deleted(client):
    voice = upload_voice(client)
    project = create_project_with_segment(client, voice["id"])
    response = client.delete(f"/api/voices/{voice['id']}")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "voice_in_use"
    # Removing the segment releases the voice.
    client.put(f"/api/projects/{project['id']}", json={"title": "Demo", "segments": []})
    assert client.delete(f"/api/voices/{voice['id']}").status_code == 200


def test_audio_field_alias_is_accepted(client):
    """The frozen SPEC says `audio`; the current UI sends `file`. Both work."""
    response = client.post(
        "/api/voices",
        data={"name": "Alias", "language": "ru", "transcript": "t", "consent": "true", "consent_basis": "own"},
        files={"audio": ("ref.wav", make_wav_bytes(3), "audio/wav")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Alias"


def test_patch_transcript_stales_takes_name_patch_does_not(client):
    voice = upload_voice(client)
    project = create_project_with_segment(client, voice["id"])
    job = wait_job(
        client, client.post(f"/api/projects/{project['id']}/generate", json={}).json()["id"]
    )
    assert job["state"] == "done"
    fresh = client.get(f"/api/projects/{project['id']}").json()
    assert fresh["segments"][0]["takes"][0]["stale"] is False

    renamed = client.patch(f"/api/voices/{voice['id']}", json={"name": "Renamed"})
    assert renamed.status_code == 200
    renamed_body = renamed.json()
    assert renamed_body["name"] == "Renamed"
    assert renamed_body["transcript"] == voice["transcript"]
    assert renamed_body["consent_basis"] == voice["consent_basis"]
    still_fresh = client.get(f"/api/projects/{project['id']}").json()
    assert still_fresh["segments"][0]["takes"][0]["stale"] is False

    patched = client.patch(
        f"/api/voices/{voice['id']}", json={"transcript": "Исправленный текст."}
    )
    assert patched.status_code == 200
    assert patched.json()["transcript"] == "Исправленный текст."
    stale = client.get(f"/api/projects/{project['id']}").json()
    assert stale["segments"][0]["takes"][0]["stale"] is True

    assert client.patch(f"/api/voices/{voice['id']}", json={"name": ""}).status_code == 422
    too_long = client.patch(f"/api/voices/{voice['id']}", json={"transcript": "x" * 5001})
    assert too_long.status_code == 422
