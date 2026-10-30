"""Stitched WAV / ZIP export with an honest manifest."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from ..audio import TARGET_SR, mix_takes
from ..config import Settings
from ..db import Database
from ..errors import ApiError
from ..storage import new_id, sha256_file
from ..util import json_loads, utcnow_iso
from .projects import compute_input_hash

APP_NAME = "Voice Atelier"


def build_export_payload(
    settings: Settings,
    db: Database,
    project_id: str,
    fmt: str,
    normalize: bool,
    allow_stale: bool,
) -> dict:
    if fmt not in ("wav", "zip"):
        raise ApiError(422, "invalid_format", "format must be wav|zip")
    project = db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    if project is None:
        raise ApiError(404, "not_found", "Project not found")
    segments = db.fetchall(
        "SELECT * FROM segments WHERE project_id = ? ORDER BY ord ASC", (project_id,)
    )
    if not segments:
        raise ApiError(422, "empty_project", "Project has no segments")
    voices = {row["id"]: row for row in db.fetchall("SELECT * FROM voices")}
    takes = {
        row["id"]: row
        for row in db.fetchall("SELECT * FROM takes WHERE project_id = ?", (project_id,))
    }
    missing: list[str] = []
    stale: list[str] = []
    items: list[dict] = []
    for index, segment in enumerate(segments):
        take_id = segment["selected_take_id"]
        take = takes.get(take_id) if take_id else None
        if take is None or not Path(take["path"]).is_file():
            missing.append(segment["id"])
            continue
        voice = voices.get(segment["voice_id"]) if segment["voice_id"] else None
        current_hash = compute_input_hash(segment, voice)
        is_stale = take["input_hash"] != current_hash
        if is_stale:
            stale.append(segment["id"])

        take_request = json_loads(take.get("request_json"), {}) or {}
        take_result = json_loads(take.get("result_json"), {}) or {}
        rendered_voice = take_request.get("voice")
        if not isinstance(rendered_voice, dict):
            if take.get("voice_id") or take_request.get("voice_ref_sha256"):
                # Legacy clone take from before voice provenance snapshots.
                rendered_voice = {
                    "id": take.get("voice_id"),
                    "name": None,
                    "consent_basis": None,
                    "consent_at": None,
                    "ref_sha256": take_request.get("voice_ref_sha256"),
                    "provenance_incomplete": True,
                }
            else:
                rendered_voice = None
        rendered = {
            "text": take_request.get("text", segment["text"]),
            "mode": take_request.get("mode", segment["mode"]),
            "model_id": take_request.get("model_id", segment["model_id"]),
            "language": take_request.get("language", segment["language"]),
            "instruction": take_request.get("instruction", ""),
            "rate": take_request.get("rate", 1.0),
            "seed": take_request.get("seed", take["seed"]),
            "voice": rendered_voice,
            "model_revision": take_result.get("model_revision"),
            "warnings": take_result.get("warnings") or [],
        }
        requested_voice = (
            {
                "id": voice["id"],
                "name": voice["name"],
                "consent_basis": voice["consent_basis"],
                "consent_at": voice["consent_at"],
                "ref_sha256": voice["ref_sha256"],
            }
            if voice
            else None
        )
        requested = {
            "text": segment["text"],
            "mode": segment["mode"],
            "model_id": segment["model_id"],
            "language": segment["language"],
            "instruction": segment["instruction"],
            "rate": segment["rate"],
            "pause_ms": segment["pause_ms"],
            "seed": segment["seed"],
            "voice": requested_voice,
        }
        items.append(
            {
                "index": index,
                "segment_id": segment["id"],
                # Top-level fields describe what is actually audible.
                "text": rendered["text"],
                "mode": rendered["mode"],
                "model_id": rendered["model_id"],
                "model_revision": rendered["model_revision"],
                "language": rendered["language"],
                "instruction": rendered["instruction"],
                "rate": rendered["rate"],
                # pause_ms is applied at mix time, so the current value is real.
                "pause_ms": segment["pause_ms"],
                "seed": rendered["seed"],
                "stale": is_stale,
                "voice": rendered_voice,
                "rendered": rendered,
                # Current editor state; only exposed when it differs from the audio.
                "requested": requested if is_stale else None,
                "take": {
                    "id": take["id"],
                    "created_at": take["created_at"],
                    "duration": take["duration"],
                    "sample_rate": take["sample_rate"],
                    "seed": take["seed"],
                    "input_hash": take["input_hash"],
                    # Internal only; stripped from the public manifest.
                    "path": take["path"],
                },
            }
        )
    if missing:
        raise ApiError(
            409,
            "missing_take",
            "Some segments have no selected take",
            {"segment_ids": missing},
        )
    if stale and not allow_stale:
        raise ApiError(
            409,
            "stale_take",
            "Some selected takes no longer match the edited segment",
            {"segment_ids": stale},
        )
    return {
        "project_id": project_id,
        "project_title": project["title"],
        "format": fmt,
        "normalize": bool(normalize),
        "allow_stale": bool(allow_stale),
        "created_at": utcnow_iso(),
        "sample_rate": TARGET_SR,
        "segments": items,
    }


def _slug(project_title: str, created_at: str) -> str:
    stamp = "".join(ch for ch in created_at if ch.isdigit())[:14]
    return f"voice-atelier-{stamp or 'export'}"


def render_export(settings: Settings, payload: dict) -> dict:
    export_id = new_id()
    fmt = payload["format"]
    base = _slug(payload["project_title"], payload["created_at"])
    segments = payload["segments"]
    take_paths = [Path(item["take"]["path"]) for item in segments]
    for path in take_paths:
        if not path.is_file():
            raise ApiError(409, "missing_take", "A take referenced by this export is missing")
    pauses = [int(item["pause_ms"]) for item in segments]

    work_dir = settings.tmp_dir / f"export-{export_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    final: Path | None = None
    try:
        mixed_path = work_dir / "mixed.wav"
        mix_info = mix_takes(take_paths, pauses, mixed_path, normalize=payload["normalize"])

        if fmt == "wav":
            final = settings.exports_dir / f"{export_id}.wav"
            shutil.copyfile(mixed_path, final)
            filename = f"{base}.wav"
        else:
            final = settings.exports_dir / f"{export_id}.zip"
            for position, item in enumerate(segments, start=1):
                item["file"] = f"segments/{position:03d}-{item['segment_id']}.wav"
                item["sha256"] = sha256_file(Path(item["take"]["path"]))
            manifest = _manifest(payload, mix_info, mixed_path, segments, take_paths)
            script = _script_text(payload["project_title"], segments)
            with zipfile.ZipFile(final, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for item in segments:
                    archive.write(Path(item["take"]["path"]), item["file"])
                archive.write(mixed_path, "mixed.wav")
                archive.writestr("script.txt", script)
                archive.writestr(
                    "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False)
                )
            filename = f"{base}.zip"

        return {
            "id": export_id,
            "format": fmt,
            "path": str(final),
            "filename": filename,
            "duration": mix_info["duration"],
            "sha256": mix_info["sha256"],
            "sample_rate": mix_info["sample_rate"],
            "normalized": mix_info["normalized"],
            "segments": len(segments),
        }
    except Exception:
        if final is not None:
            try:
                final.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _script_text(title: str, segments: list[dict]) -> str:
    lines = [f"{APP_NAME} — {title}", ""]
    for position, item in enumerate(segments, start=1):
        rendered = item.get("rendered") or {}
        speaker = (
            (rendered.get("voice") or {}).get("name")
            or (item.get("voice") or {}).get("name")
            or item["mode"]
        )
        lines.append(f"[{position:03d}] ({speaker}) {item['text']}")
        requested = item.get("requested")
        if requested and requested.get("text") != item["text"]:
            lines.append(f"# requested but not narrated: {requested['text']}")
        lines.append("")
    return "\n".join(lines)


def _manifest(
    payload: dict,
    mix_info: dict,
    mixed_path: Path,
    segments: list[dict],
    take_paths: list[Path],
) -> dict:
    outputs = {
        "mixed": {
            "file": "mixed.wav",
            "sha256": sha256_file(mixed_path),
            "duration": mix_info["duration"],
            "sample_rate": mix_info["sample_rate"],
            "normalized": mix_info["normalized"],
        }
    }
    return {
        "app": APP_NAME,
        "created_at": payload["created_at"],
        "format": payload["format"],
        "sample_rate": payload["sample_rate"],
        "normalize": payload["normalize"],
        "normalize_note": "peak normalization only; not EBU R128 loudness mastering"
        if payload["normalize"]
        else "none",
        "project": {"id": payload["project_id"], "title": payload["project_title"]},
        "notes": [
            "rate changes are pitch-preserving ffmpeg atempo post-processes",
            "pause_ms is inserted digital silence after the segment",
            "seeds are best-effort, not guaranteed bit-exact",
            "no denoise or enhancement was applied",
            "top-level segment fields and rendered.* describe the audio that was actually "
            "rendered; requested.* (only when stale_at_export) is the editor state at export",
        ],
        "segments": [
            _manifest_segment(item)
            for item in segments
        ],
        "outputs": outputs,
    }


def _manifest_segment(item: dict) -> dict:
    take = item["take"]
    entry = {
        "index": item["index"],
        "segment_id": item["segment_id"],
        "text": item["text"],
        "mode": item["mode"],
        "model_id": item["model_id"],
        "model_revision": item["model_revision"],
        "language": item["language"],
        "instruction": item["instruction"],
        "rate": item["rate"],
        "pause_ms": item["pause_ms"],
        "seed": item["seed"],
        "stale_at_export": item["stale"],
        "voice": item.get("voice"),
        "rendered": item.get("rendered"),
        # No filesystem paths in the public manifest.
        "take": {
            "id": take["id"],
            "created_at": take["created_at"],
            "duration": take["duration"],
            "sample_rate": take["sample_rate"],
            "seed": take["seed"],
            "input_hash": take["input_hash"],
            "audio_sha256": sha256_file(Path(take["path"])),
        },
        "file": item.get("file"),
        "take_sha256": sha256_file(Path(take["path"])),
    }
    if item.get("stale") and item.get("requested"):
        entry["requested"] = item["requested"]
    return entry


def record_export(db: Database, project_id: str, job_id: str, info: dict) -> None:
    db.execute(
        "INSERT INTO exports (id,project_id,job_id,format,path,filename,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            info["id"],
            project_id,
            job_id,
            info["format"],
            info["path"],
            info["filename"],
            utcnow_iso(),
        ),
    )


def export_path(db: Database, export_id: str) -> tuple[Path, str, str]:
    row = db.fetchone("SELECT * FROM exports WHERE id = ?", (export_id,))
    if row is None:
        raise ApiError(404, "not_found", "Export not found")
    path = Path(row["path"])
    if not path.is_file():
        raise ApiError(404, "not_found", "Export file is missing")
    filename = row.get("filename") or f"voice-atelier-{export_id[:8]}.{row['format']}"
    return path, row["format"], filename


def download_name(payload_title: str, export_id: str, fmt: str) -> str:
    return f"voice-atelier-{export_id[:8]}.{fmt}"
