"""Projects, segments, takes, staleness and generation snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from ..config import DESIGN_MAX_CHARS, SUPPORTED_LANGUAGES, Settings
from ..db import Database
from ..engine.catalog import (
    CLONE_MODEL_IDS,
    DEFAULT_CLONE_MODEL,
    DEFAULT_DESIGN_MODEL,
    DESIGN_MODEL_IDS,
    MODELS_BY_ID,
    installed_state,
)
from ..errors import ApiError
from ..storage import new_id
from ..util import json_dumps, json_loads, utcnow_iso

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MAX_PAUSE_MS = 5000
MIN_RATE = 0.5
MAX_RATE = 2.0


def summary(row: dict) -> dict:
    return {"id": row["id"], "title": row["title"], "updated_at": row["updated_at"]}


def public_take(row: dict, current_hash: str) -> dict:
    result = json_loads(row.get("result_json"), {}) or {}
    warnings = result.get("warnings") if isinstance(result, dict) else None
    return {
        "id": row["id"],
        "segment_id": row["segment_id"],
        "created_at": row["created_at"],
        "duration": row["duration"],
        "seed": row["seed"],
        "model_id": row["model_id"],
        "input_hash": row["input_hash"],
        "stale": row["input_hash"] != current_hash,
        # e.g. possible_truncation_heuristic / possible_runaway_repetition; UI must localize.
        "warnings": list(warnings) if isinstance(warnings, list) else [],
    }


def compute_input_hash(segment: dict, voice: dict | None) -> str:
    """Hash the actual render inputs; edited text/voice/seed/rate makes takes stale."""
    mode = segment["mode"]
    payload = {
        "v": 1,
        "text": segment["text"],
        "mode": mode,
        "model_id": segment["model_id"],
        "language": segment["language"],
        "instruction": segment["instruction"] if mode == "design" else "",
        "seed": int(segment["seed"]),
        "rate": round(float(segment["rate"]), 3),
        "voice_ref": voice["ref_sha256"] if (mode == "clone" and voice) else None,
        "voice_transcript": voice["transcript"] if (mode == "clone" and voice) else None,
    }
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def _voices_map(db: Database) -> dict[str, dict]:
    return {row["id"]: row for row in db.fetchall("SELECT * FROM voices")}


def get_project(db: Database, project_id: str) -> dict:
    project = db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise ApiError(404, "not_found", "Project not found")
    voices = _voices_map(db)
    segments = db.fetchall(
        "SELECT * FROM segments WHERE project_id = ? ORDER BY ord ASC", (project_id,)
    )
    takes_by_segment: dict[str, list[dict]] = {}
    for take in db.fetchall(
        "SELECT * FROM takes WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    ):
        takes_by_segment.setdefault(take["segment_id"], []).append(take)
    out_segments = []
    for segment in segments:
        voice = voices.get(segment["voice_id"]) if segment["voice_id"] else None
        current_hash = compute_input_hash(segment, voice)
        out_segments.append(
            {
                "id": segment["id"],
                "text": segment["text"],
                "voice_id": segment["voice_id"],
                "mode": segment["mode"],
                "model_id": segment["model_id"],
                "language": segment["language"],
                "instruction": segment["instruction"],
                "rate": segment["rate"],
                "pause_ms": segment["pause_ms"],
                "seed": segment["seed"],
                "selected_take_id": segment["selected_take_id"],
                "takes": [
                    public_take(take, current_hash)
                    for take in takes_by_segment.get(segment["id"], [])
                ],
            }
        )
    return {
        "id": project["id"],
        "title": project["title"],
        "created_at": project["created_at"],
        "updated_at": project["updated_at"],
        "segments": out_segments,
    }


def list_projects(db: Database) -> list[dict]:
    rows = db.fetchall("SELECT * FROM projects ORDER BY updated_at DESC")
    return [summary(row) for row in rows]


def create_project(db: Database, title: str) -> dict:
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise ApiError(422, "invalid_title", "Title must be 1..200 characters")
    project_id = new_id()
    now = utcnow_iso()
    db.execute(
        "INSERT INTO projects (id,title,created_at,updated_at) VALUES (?,?,?,?)",
        (project_id, title, now, now),
    )
    return get_project(db, project_id)


def delete_project(settings: Settings, db: Database, project_id: str) -> None:
    exists = db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,))
    if exists is None:
        raise ApiError(404, "not_found", "Project not found")
    takes = db.fetchall("SELECT path FROM takes WHERE project_id = ?", (project_id,))
    exports = db.fetchall("SELECT path FROM exports WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM takes WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM segments WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM exports WHERE project_id = ?", (project_id,))
    db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    for take in takes:
        try:
            Path(take["path"]).unlink()
        except OSError:
            pass
    for export in exports:
        try:
            Path(export["path"]).unlink()
        except OSError:
            pass


def _normalize_input(db: Database, data: dict, settings: Settings) -> dict:
    mode = (data.get("mode") or "clone").strip()
    if mode not in ("clone", "design"):
        raise ApiError(422, "invalid_mode", "mode must be clone|design")

    model_id = (data.get("model_id") or "").strip()
    if not model_id:
        model_id = DEFAULT_CLONE_MODEL if mode == "clone" else DEFAULT_DESIGN_MODEL
    if model_id not in MODELS_BY_ID:
        raise ApiError(422, "invalid_model", f"Unknown model_id: {model_id}")
    spec = MODELS_BY_ID[model_id]
    if spec.kind != mode:
        raise ApiError(422, "invalid_model", f"Model {model_id} cannot be used in {mode} mode")

    language = (data.get("language") or "ru").strip()
    if language not in SUPPORTED_LANGUAGES:
        raise ApiError(422, "invalid_language", "Unsupported language")

    text = data.get("text")
    text = "" if text is None else str(text)
    if len(text) > settings.max_text_chars:
        raise ApiError(422, "text_too_long", f"Segment text exceeds {settings.max_text_chars} chars")
    if mode == "design" and len(text) > DESIGN_MAX_CHARS:
        raise ApiError(
            422,
            "design_text_too_long",
            f"Design segments are limited to {DESIGN_MAX_CHARS} characters because each design "
            "call creates a new identity; save the designed voice, then clone longer text",
        )

    instruction = data.get("instruction")
    instruction = "" if instruction is None else str(instruction)
    if len(instruction) > settings.max_instruction_chars:
        raise ApiError(
            422, "instruction_too_long", f"Instruction exceeds {settings.max_instruction_chars} chars"
        )
    if mode == "clone" and instruction.strip():
        raise ApiError(
            422,
            "clone_instruction_not_supported",
            "The clone model has no free-form instruction control; leave instruction empty",
        )
    if mode == "design":
        instruction = instruction.strip()

    rate = data.get("rate", 1.0)
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_rate", "rate must be a number")
    if not math.isfinite(rate) or not (MIN_RATE <= rate <= MAX_RATE):
        raise ApiError(422, "invalid_rate", f"rate must be within {MIN_RATE}..{MAX_RATE}")

    pause_ms = data.get("pause_ms", 300)
    try:
        pause_ms = int(pause_ms)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_pause", "pause_ms must be an integer")
    if not (0 <= pause_ms <= MAX_PAUSE_MS):
        raise ApiError(422, "invalid_pause", f"pause_ms must be within 0..{MAX_PAUSE_MS}")

    seed = data.get("seed", 42)
    try:
        seed = int(seed)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_seed", "seed must be an integer")
    if not (0 <= seed <= 2**31 - 1):
        raise ApiError(422, "invalid_seed", "seed must be within 0..2^31-1")

    voice_id = data.get("voice_id")
    if mode == "design":
        voice_id = None
    if voice_id is not None:
        voice_id = str(voice_id)
        if db.fetchone("SELECT id FROM voices WHERE id = ?", (voice_id,)) is None:
            raise ApiError(422, "invalid_voice", "Voice not found")

    return {
        "text": text,
        "voice_id": voice_id,
        "mode": mode,
        "model_id": model_id,
        "language": language,
        "instruction": instruction,
        "rate": rate,
        "pause_ms": pause_ms,
        "seed": seed,
    }


def save_project(settings: Settings, db: Database, project_id: str, title: str, segments: list[dict]) -> dict:
    project = db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise ApiError(404, "not_found", "Project not found")
    title = (title if title is not None else project["title"]).strip()
    if not title or len(title) > 200:
        raise ApiError(422, "invalid_title", "Title must be 1..200 characters")
    if len(segments) > settings.max_segments:
        raise ApiError(422, "too_many_segments", f"At most {settings.max_segments} segments")

    normalized: list[tuple[str, dict, str | None]] = []
    seen_ids: set[str] = set()
    for item in segments:
        if not isinstance(item, dict):
            raise ApiError(422, "invalid_request", "Each segment must be an object")
        seg_id = item.get("id")
        if seg_id is not None:
            seg_id = str(seg_id)
            if not ID_RE.match(seg_id):
                raise ApiError(422, "invalid_segment_id", "Segment id must be a UUID-like string")
            if seg_id in seen_ids:
                raise ApiError(422, "duplicate_segment_id", f"Duplicate segment id: {seg_id}")
            seen_ids.add(seg_id)
        selected = item.get("selected_take_id")
        if selected is not None:
            selected = str(selected)
        normalized.append((seg_id, _normalize_input(db, item, settings), selected))

    now = utcnow_iso()
    removed_ids: list[str] = []
    removed_paths: list[Path] = []
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing_rows = {
            row["id"]: dict(row)
            for row in conn.execute("SELECT * FROM segments WHERE project_id = ?", (project_id,))
        }
        other_rows = {}
        ids = [seg_id for seg_id, _, _ in normalized if seg_id]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            other_rows = {
                row["id"]: dict(row)
                for row in conn.execute(
                    f"SELECT * FROM segments WHERE id IN ({placeholders}) AND project_id != ?",
                    (*ids, project_id),
                )
            }
            if other_rows:
                conflict = sorted(other_rows)[0]
                raise ApiError(422, "invalid_segment_id", f"Segment id belongs to another project: {conflict}")

        incoming_ids = {seg_id for seg_id, _, _ in normalized if seg_id}
        for seg_id, row in existing_rows.items():
            if seg_id not in incoming_ids:
                removed_ids.append(seg_id)

        for index, (seg_id, fields, selected) in enumerate(normalized):
            existing = existing_rows.get(seg_id) if seg_id else None
            if existing is not None:
                target_id = seg_id
                keep_selected = existing["selected_take_id"]
                if selected is not None:
                    take = conn.execute(
                        "SELECT id FROM takes WHERE id = ? AND segment_id = ?", (selected, target_id)
                    ).fetchone()
                    if take is None:
                        raise ApiError(422, "invalid_take", "selected_take_id does not belong to this segment")
                    keep_selected = selected
                conn.execute(
                    "UPDATE segments SET ord=?, text=?, voice_id=?, mode=?, model_id=?, language=?,"
                    " instruction=?, rate=?, pause_ms=?, seed=?, selected_take_id=? WHERE id=?",
                    (
                        index,
                        fields["text"],
                        fields["voice_id"],
                        fields["mode"],
                        fields["model_id"],
                        fields["language"],
                        fields["instruction"],
                        fields["rate"],
                        fields["pause_ms"],
                        fields["seed"],
                        keep_selected,
                        target_id,
                    ),
                )
            else:
                target_id = seg_id or new_id()
                selected_value = None
                if selected is not None:
                    raise ApiError(422, "invalid_take", "A new segment cannot select an existing take")
                conn.execute(
                    "INSERT INTO segments (id,project_id,ord,text,voice_id,mode,model_id,language,"
                    "instruction,rate,pause_ms,seed,selected_take_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        target_id,
                        project_id,
                        index,
                        fields["text"],
                        fields["voice_id"],
                        fields["mode"],
                        fields["model_id"],
                        fields["language"],
                        fields["instruction"],
                        fields["rate"],
                        fields["pause_ms"],
                        fields["seed"],
                        selected_value,
                    ),
                )

        if removed_ids:
            placeholders = ",".join("?" for _ in removed_ids)
            for row in conn.execute(
                f"SELECT path FROM takes WHERE segment_id IN ({placeholders})", tuple(removed_ids)
            ):
                removed_paths.append(Path(row["path"]))
            conn.execute(f"DELETE FROM takes WHERE segment_id IN ({placeholders})", tuple(removed_ids))
            conn.execute(f"DELETE FROM segments WHERE id IN ({placeholders})", tuple(removed_ids))

        conn.execute(
            "UPDATE projects SET title=?, updated_at=? WHERE id=?", (title, now, project_id)
        )
        conn.commit()

    for path in removed_paths:
        try:
            path.unlink()
        except OSError:
            pass
    return get_project(db, project_id)


def select_take(db: Database, project_id: str, segment_id: str, take_id: str) -> dict:
    segment = db.fetchone(
        "SELECT * FROM segments WHERE id = ? AND project_id = ?", (segment_id, project_id)
    )
    if segment is None:
        raise ApiError(404, "not_found", "Segment not found in this project")
    take = db.fetchone("SELECT * FROM takes WHERE id = ? AND segment_id = ?", (take_id, segment_id))
    if take is None:
        raise ApiError(422, "invalid_take", "Take does not belong to this segment")
    db.execute(
        "UPDATE segments SET selected_take_id=? WHERE id=?", (take_id, segment_id)
    )
    db.execute("UPDATE projects SET updated_at=? WHERE id=?", (utcnow_iso(), project_id))
    return get_project(db, project_id)


def take_audio_path(db: Database, take_id: str) -> Path:
    row = db.fetchone("SELECT path FROM takes WHERE id = ?", (take_id,))
    if row is None:
        raise ApiError(404, "not_found", "Take not found")
    path = Path(row["path"])
    if not path.is_file():
        raise ApiError(404, "not_found", "Take audio is missing")
    return path


def list_designs(db: Database, limit: int = 100) -> list[dict]:
    rows = db.fetchall(
        "SELECT * FROM takes WHERE kind='design' ORDER BY created_at DESC LIMIT ?", (limit,)
    )
    items = []
    for row in rows:
        request = json_loads(row["request_json"], {}) or {}
        items.append(
            {
                "take_id": row["id"],
                "voice_id": row["voice_id"],
                "created_at": row["created_at"],
                "duration": row["duration"],
                "model_id": row["model_id"],
                "text": request.get("text", ""),
                "instruction": request.get("instruction", ""),
                "language": request.get("language", "ru"),
                "seed": row["seed"],
            }
        )
    return items


def voice_snapshot(voice: dict | None) -> dict | None:
    if voice is None:
        return None
    return {
        "id": voice["id"],
        "name": voice["name"],
        "ref_path": voice["ref_path"],
        "ref_sha256": voice["ref_sha256"],
        "transcript": voice["transcript"],
        "language": voice["language"],
        "consent_basis": voice["consent_basis"],
        "consent_at": voice["consent_at"],
    }


def build_generation_targets(
    settings: Settings,
    db: Database,
    project_id: str,
    segment_ids: list[str] | None,
    force: bool,
) -> tuple[list[dict], list[str]]:
    """Validate readiness and snapshot inputs at enqueue time."""
    project = db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise ApiError(404, "not_found", "Project not found")
    segments = db.fetchall(
        "SELECT * FROM segments WHERE project_id = ? ORDER BY ord ASC", (project_id,)
    )
    if not segments:
        raise ApiError(422, "empty_project", "Project has no segments")
    by_id = {row["id"]: row for row in segments}
    if segment_ids is not None:
        unknown = [sid for sid in segment_ids if sid not in by_id]
        if unknown:
            raise ApiError(422, "invalid_segment", "segment_ids contains unknown segments")
        chosen = [by_id[sid] for sid in segment_ids]
    else:
        chosen = list(segments)
    if not chosen:
        raise ApiError(422, "invalid_segment", "No segments selected")

    voices = _voices_map(db)
    takes_by_segment: dict[str, list[dict]] = {}
    for take in db.fetchall("SELECT * FROM takes WHERE project_id = ?", (project_id,)):
        takes_by_segment.setdefault(take["segment_id"], []).append(take)

    targets: list[dict] = []
    skipped: list[str] = []
    for segment in chosen:
        voice = voices.get(segment["voice_id"]) if segment["voice_id"] else None
        current_hash = compute_input_hash(segment, voice)
        text = (segment["text"] or "").strip()
        if not text:
            raise ApiError(
                422,
                "empty_text",
                "Segment text is empty",
                {"segment_id": segment["id"]},
            )
        if segment["mode"] == "design" and len(text) > DESIGN_MAX_CHARS:
            raise ApiError(
                422,
                "design_text_too_long",
                f"Design segments are limited to {DESIGN_MAX_CHARS} characters; save the designed "
                "voice, then clone longer text with a Base model",
                {"segment_id": segment["id"]},
            )
        if segment["mode"] == "clone":
            if segment["voice_id"] is None or voice is None:
                raise ApiError(
                    422,
                    "missing_voice",
                    "Clone segment has no voice assigned",
                    {"segment_id": segment["id"]},
                )
            if not (voice["transcript"] or "").strip():
                raise ApiError(
                    422,
                    "missing_transcript",
                    "Clone voice has no transcript; cloning requires the reference transcript",
                    {"segment_id": segment["id"]},
                )
        else:
            if not (segment["instruction"] or "").strip():
                raise ApiError(
                    422,
                    "missing_instruction",
                    "Design segment needs an instruction describing the new voice",
                    {"segment_id": segment["id"]},
                )
        spec = MODELS_BY_ID.get(segment["model_id"])
        if spec is None:
            raise ApiError(422, "invalid_model", "Unknown model")
        installed, _status, missing = installed_state(settings, spec)
        if not installed:
            raise ApiError(
                409,
                "model_not_installed",
                f"Model {spec.id} is not installed",
                {"model_id": spec.id, "missing": missing},
            )
        if not force:
            selected = segment["selected_take_id"]
            take = None
            if selected:
                take = next(
                    (t for t in takes_by_segment.get(segment["id"], []) if t["id"] == selected),
                    None,
                )
            if take is not None and take["input_hash"] == current_hash and Path(take["path"]).is_file():
                skipped.append(segment["id"])
                continue
        targets.append(
            {
                "segment_id": segment["id"],
                "segment": {
                    "text": segment["text"],
                    "mode": segment["mode"],
                    "model_id": segment["model_id"],
                    "language": segment["language"],
                    "instruction": segment["instruction"],
                    "rate": segment["rate"],
                    "seed": segment["seed"],
                    "pause_ms": segment["pause_ms"],
                    "voice_id": segment["voice_id"],
                },
                "voice": voice_snapshot(voice),
                "input_hash": current_hash,
            }
        )
    return targets, skipped
