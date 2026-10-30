"""Voice library: upload/trim/quality and synthetic (designed) voices."""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from ..audio import (
    analyze,
    decode_to_wav,
    duration_seconds,
    load_audio,
    probe_duration,
    validate_reference_source,
    write_wav,
)
from ..config import SUPPORTED_LANGUAGES, Settings
from ..db import Database
from ..errors import ApiError
from ..storage import new_id, sha256_file
from ..util import utcnow_iso

CONSENT_BASES = ("own", "permission", "licensed", "synthetic")


def public_voice(row: dict) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "language": row["language"],
        "transcript": row["transcript"],
        "consent_basis": row["consent_basis"],
        "consent_at": row["consent_at"],
        "created_at": row["created_at"],
        "duration": row["duration"],
        "sample_rate": row["sample_rate"],
        "quality": json.loads(row["quality_json"]),
        "source": row["source"],
    }


def get_voice(db: Database, voice_id: str) -> dict:
    row = db.fetchone("SELECT * FROM voices WHERE id = ?", (voice_id,))
    if row is None:
        raise ApiError(404, "not_found", "Voice not found")
    return row


def list_voices(db: Database) -> list[dict]:
    rows = db.fetchall("SELECT * FROM voices ORDER BY created_at DESC")
    return [public_voice(row) for row in rows]


def create_from_upload(
    settings: Settings,
    db: Database,
    source_path: Path,
    *,
    name: str,
    language: str,
    transcript: str,
    consent: bool,
    consent_basis: str,
    trim_start: float = 0.0,
    trim_end: float | None = None,
) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise ApiError(422, "invalid_name", "Name must be 1..120 characters")
    if language not in SUPPORTED_LANGUAGES:
        raise ApiError(422, "invalid_language", "Unsupported language")
    if not consent:
        raise ApiError(422, "consent_required", "Rights/permission acknowledgement is required")
    if consent_basis not in ("own", "permission", "licensed"):
        raise ApiError(422, "invalid_consent_basis", "consent_basis must be own|permission|licensed")
    if trim_start < 0 or not math.isfinite(trim_start):
        raise ApiError(422, "invalid_trim", "trim_start must be a finite value >= 0")
    if trim_end is not None and (not math.isfinite(trim_end) or trim_end <= trim_start):
        raise ApiError(422, "invalid_trim", "trim_end must be greater than trim_start")

    if transcript is None:
        transcript = ""
    transcript = transcript.strip()
    if len(transcript) > 5000:
        raise ApiError(422, "invalid_transcript", "Transcript is too long")

    voice_id = new_id()
    voice_dir = settings.voices_dir / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)
    raw_wav = settings.tmp_dir / f"{voice_id}.raw.wav"
    ref_path = voice_dir / "reference.wav"
    try:
        validate_reference_source(source_path)
        probe = probe_duration(source_path)
        if probe is not None and probe > settings.max_ref_seconds:
            raise ApiError(
                422,
                "reference_too_long",
                f"Reference is {probe:.1f}s; maximum is {settings.max_ref_seconds:.0f}s",
            )
        decode_to_wav(
            source_path,
            raw_wav,
            timeout=settings.subprocess_timeout,
            max_seconds=settings.max_ref_seconds,
        )
        audio, sr = load_audio(raw_wav)
        if duration_seconds(audio, sr) > settings.max_ref_seconds + 0.25:
            raise ApiError(
                422,
                "reference_too_long",
                f"Reference exceeds {settings.max_ref_seconds:.0f}s after decoding",
            )
        start = max(0, int(round(trim_start * sr)))
        end = len(audio) if trim_end is None else min(len(audio), int(round(trim_end * sr)))
        if end <= start:
            raise ApiError(422, "invalid_trim", "Trim range is empty")
        selected = audio[start:end]
        duration = duration_seconds(selected, sr)
        if duration < settings.min_ref_seconds:
            raise ApiError(
                422,
                "reference_too_short",
                f"Selected reference is {duration:.2f}s; minimum is {settings.min_ref_seconds:.0f}s",
            )
        if duration > settings.max_selected_seconds:
            raise ApiError(
                422,
                "reference_selection_too_long",
                f"Selected reference is {duration:.1f}s; trim it to {settings.max_selected_seconds:.0f}s or less",
            )
        write_wav(ref_path, selected, sr)
        quality = analyze(selected, sr)
        digest = sha256_file(ref_path)
        now = utcnow_iso()
        db.execute(
            "INSERT INTO voices (id,name,language,transcript,consent_basis,consent_at,created_at,"
            "source,duration,sample_rate,ref_path,ref_sha256,quality_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                voice_id,
                name,
                language,
                transcript,
                consent_basis,
                now,
                now,
                "upload",
                round(duration, 3),
                sr,
                str(ref_path),
                digest,
                json.dumps(quality),
            ),
        )
        return public_voice(get_voice(db, voice_id))
    except Exception:
        shutil.rmtree(voice_dir, ignore_errors=True)
        raise
    finally:
        for path in (raw_wav,):
            try:
                path.unlink()
            except OSError:
                pass


def create_synthetic_voice(
    settings: Settings,
    db: Database,
    take_wav: Path,
    *,
    text: str,
    language: str,
    name: str,
) -> dict:
    """Reference built from our own generated clip (voice design)."""
    audio, sr = load_audio(take_wav)
    if duration_seconds(audio, sr) < 0.5:
        raise ApiError(
            422,
            "design_clip_too_short",
            "Designed clip is too short (under 0.5 s) to reuse as a voice reference; "
            "use a longer design text",
        )
    voice_id = new_id()
    voice_dir = settings.voices_dir / voice_id
    voice_dir.mkdir(parents=True, exist_ok=True)
    ref_path = voice_dir / "reference.wav"
    write_wav(ref_path, audio, sr)
    quality = analyze(audio, sr)
    now = utcnow_iso()
    db.execute(
        "INSERT INTO voices (id,name,language,transcript,consent_basis,consent_at,created_at,"
        "source,duration,sample_rate,ref_path,ref_sha256,quality_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            voice_id,
            (name or "Designed voice").strip()[:120],
            language,
            text,
            "synthetic",
            now,
            now,
            "design",
            round(duration_seconds(audio, sr), 3),
            sr,
            str(ref_path),
            sha256_file(ref_path),
            json.dumps(quality),
        ),
    )
    return public_voice(get_voice(db, voice_id))


def update_voice(
    db: Database,
    voice_id: str,
    *,
    name: str | None = None,
    transcript: str | None = None,
) -> dict:
    """Rename/repair transcript only; reference audio and consent are immutable.

    The reference transcript is part of the clone input hash, so editing it
    naturally marks existing takes stale; queued jobs keep their old snapshot.
    """
    row = get_voice(db, voice_id)
    new_name = row["name"]
    new_transcript = row["transcript"]
    if name is not None:
        new_name = name.strip()
        if not new_name or len(new_name) > 120:
            raise ApiError(422, "invalid_name", "Name must be 1..120 characters")
    if transcript is not None:
        new_transcript = transcript.strip()
        if len(new_transcript) > 5000:
            raise ApiError(422, "invalid_transcript", "Transcript is too long")
    if new_name != row["name"] or new_transcript != row["transcript"]:
        db.execute(
            "UPDATE voices SET name = ?, transcript = ? WHERE id = ?",
            (new_name, new_transcript, voice_id),
        )
    return public_voice(get_voice(db, voice_id))


def delete_voice(settings: Settings, db: Database, voice_id: str) -> None:
    row = get_voice(db, voice_id)
    used = db.fetchone("SELECT id FROM segments WHERE voice_id = ? LIMIT 1", (voice_id,))
    if used is not None:
        raise ApiError(409, "voice_in_use", "Voice is used by a project segment")
    # Queued/running jobs snapshot the reference path; block deletion while used.
    active = db.fetchall("SELECT payload_json FROM jobs WHERE state IN ('queued','running')")
    for job in active:
        try:
            payload = json.loads(job["payload_json"])
        except ValueError:
            continue
        if _payload_mentions_voice(payload, voice_id):
            raise ApiError(409, "voice_in_use", "Voice is referenced by an active job")
    # Design takes keep their own audio; dropping the voice link is non-destructive.
    db.execute("UPDATE takes SET voice_id = NULL WHERE voice_id = ?", (voice_id,))
    db.execute("DELETE FROM voices WHERE id = ?", (voice_id,))
    shutil.rmtree(Path(row["ref_path"]).parent, ignore_errors=True)


def _payload_mentions_voice(payload, voice_id: str) -> bool:
    if isinstance(payload, dict):
        if payload.get("voice_id") == voice_id:
            return True
        return any(_payload_mentions_voice(value, voice_id) for value in payload.values())
    if isinstance(payload, list):
        return any(_payload_mentions_voice(value, voice_id) for value in payload)
    return False


def audio_path(db: Database, voice_id: str) -> Path:
    row = get_voice(db, voice_id)
    path = Path(row["ref_path"])
    if not path.is_file():
        raise ApiError(404, "not_found", "Voice audio is missing")
    return path
