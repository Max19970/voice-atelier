"""Job handlers: generate, design, download, unload, export."""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from pathlib import Path

from ..audio import TARGET_SR
from ..config import Settings
from ..db import Database
from ..engine.base import SynthRequest
from ..engine.catalog import MODELS_BY_ID
from ..engine.downloader import download_model, read_manifest
from ..engine.manager import EngineManager
from ..errors import ApiError, EngineCancelled, EngineError, JobCanceled
from ..jobs import JobControls
from ..storage import new_id
from ..util import json_dumps, utcnow_iso
from . import export as export_service
from . import projects, voices


class Pipeline:
    def __init__(self, settings: Settings, db: Database, manager: EngineManager) -> None:
        self.settings = settings
        self.db = db
        self.manager = manager

    @property
    def handlers(self) -> dict:
        return {
            "generate": self.generate,
            "design": self.design,
            "download": self.download,
            "unload": self.unload,
            "export": self.export,
        }

    # ---- generate ------------------------------------------------------
    def generate(self, ctl: JobControls, payload: dict) -> None:
        project_id = payload["project_id"]
        targets = payload.get("targets") or []
        skipped = payload.get("skipped") or []
        total = len(targets)
        generated: list[str] = []
        failed: list[dict] = []
        notes: list[str] = []
        if total == 0:
            ctl.set_result(
                {"project_id": project_id, "generated": [], "skipped": skipped, "failed": []}
            )
            return

        for index, target in enumerate(targets):
            ctl.check()
            segment_id = target["segment_id"]
            segment = target["segment"]
            voice = target.get("voice")
            snapshot_hash = target["input_hash"]
            temp_path = self.settings.tmp_dir / f"{uuid.uuid4().hex}.wav"
            try:
                self.manager.ensure(segment["model_id"])
                request = SynthRequest(
                    text=segment["text"],
                    language=segment["language"],
                    mode=segment["mode"],
                    model_id=segment["model_id"],
                    out_path=temp_path,
                    seed=int(segment["seed"]),
                    rate=float(segment["rate"]),
                    instruction=segment["instruction"] if segment["mode"] == "design" else "",
                    ref_audio_path=Path(voice["ref_path"]) if voice and voice.get("ref_path") else None,
                    ref_text=voice["transcript"] if voice else None,
                    chunk_chars=self.settings.chunk_chars,
                )

                def progress(fraction: float, stage: str, _index=index) -> None:
                    ctl.progress((_index + max(0.0, min(1.0, fraction))) / total, stage)

                result = self.manager.adapter().synthesize(
                    request, progress, ctl.cancelled
                )
                if not temp_path.is_file() or temp_path.stat().st_size == 0:
                    raise EngineError("empty_output", "Engine produced no audio file")
                # Cancellation after the GPU call must win before committing.
                if ctl.cancelled():
                    raise JobCanceled()
                if self.db.fetchone("SELECT id FROM projects WHERE id = ?", (project_id,)) is None:
                    raise EngineError(
                        "project_deleted", "Project was deleted during generation"
                    )
                if self.db.fetchone("SELECT id FROM segments WHERE id = ?", (segment_id,)) is None:
                    raise EngineError(
                        "segment_deleted", "Segment was deleted during generation"
                    )

                take_id = new_id()
                final_path = self.settings.takes_dir / f"{take_id}.wav"
                os.replace(temp_path, final_path)
                manifest = read_manifest(self.settings, segment["model_id"]) or {}
                request_snapshot = {
                    "text": segment["text"],
                    "language": segment["language"],
                    "mode": segment["mode"],
                    "model_id": segment["model_id"],
                    "instruction": segment["instruction"],
                    "seed": int(segment["seed"]),
                    "rate": float(segment["rate"]),
                    "voice_id": segment.get("voice_id"),
                    "voice_ref_sha256": voice.get("ref_sha256") if voice else None,
                    # Provenance survives later voice renames/deletion.
                    "voice": (
                        {
                            "id": voice.get("id"),
                            "name": voice.get("name"),
                            "consent_basis": voice.get("consent_basis"),
                            "consent_at": voice.get("consent_at"),
                            "ref_sha256": voice.get("ref_sha256"),
                        }
                        if voice
                        else None
                    ),
                    "chunk_chars": self.settings.chunk_chars,
                }
                result_snapshot = {
                    "sample_rate": result.sample_rate,
                    "duration": result.duration,
                    "chunks": result.chunks,
                    "warnings": result.warnings,
                    "rate_applied": result.rate_applied,
                    "engine": result.engine_info,
                    "model_revision": manifest.get("revision"),
                }
                try:
                    self.db.execute(
                        "INSERT INTO takes (id,project_id,segment_id,voice_id,kind,path,created_at,"
                        "duration,sample_rate,seed,model_id,input_hash,request_json,result_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            take_id,
                            project_id,
                            segment_id,
                            segment.get("voice_id"),
                            "segment",
                            str(final_path),
                            utcnow_iso(),
                            result.duration,
                            result.sample_rate,
                            int(segment["seed"]),
                            segment["model_id"],
                            snapshot_hash,
                            json_dumps(request_snapshot),
                            json_dumps(result_snapshot),
                        ),
                    )
                except sqlite3.IntegrityError:
                    _unlink(final_path)
                    raise EngineError(
                        "segment_deleted", "Target was deleted during generation"
                    ) from None

                # Only auto-select if the segment still matches the snapshot.
                current = self.db.fetchone(
                    "SELECT * FROM segments WHERE id = ? AND project_id = ?",
                    (segment_id, project_id),
                )
                if current is None:
                    notes.append(f"segment {segment_id} deleted during generation; take kept unselected")
                else:
                    current_voice = (
                        self.db.fetchone("SELECT * FROM voices WHERE id = ?", (current["voice_id"],))
                        if current["voice_id"]
                        else None
                    )
                    if projects.compute_input_hash(current, current_voice) == snapshot_hash:
                        self.db.execute(
                            "UPDATE segments SET selected_take_id = ? WHERE id = ?",
                            (take_id, segment_id),
                        )
                    else:
                        notes.append(
                            f"segment {segment_id} changed during generation; take kept unselected (stale)"
                        )
                generated.append(take_id)
            except (JobCanceled, EngineCancelled):
                _unlink(temp_path)
                raise JobCanceled()
            except (ApiError, EngineError) as exc:
                _unlink(temp_path)
                failed.append(
                    {"segment_id": segment_id, "code": exc.code, "message": exc.message}
                )
                ctl.progress((index + 1) / total, f"failed segment {index + 1}/{total}")
            finally:
                _unlink(temp_path)

        if not generated and failed:
            first = failed[0]
            raise ApiError(409, first["code"], first["message"], {"failed": failed})
        if notes:
            for note in notes:
                _ = note  # notes remain visible in logs below via result
        ctl.set_result(
            {
                "project_id": project_id,
                "generated": generated,
                "skipped": skipped,
                "failed": failed,
            }
        )

    # ---- design --------------------------------------------------------
    def design(self, ctl: JobControls, payload: dict) -> None:
        ctl.check()
        model_id = payload["model_id"]
        temp_path = self.settings.tmp_dir / f"{uuid.uuid4().hex}.wav"
        try:
            self.manager.ensure(model_id)
            request = SynthRequest(
                text=payload["text"],
                language=payload["language"],
                mode="design",
                model_id=model_id,
                out_path=temp_path,
                seed=int(payload["seed"]),
                rate=float(payload["rate"]),
                instruction=payload["instruction"],
                chunk_chars=self.settings.chunk_chars,
            )
            result = self.manager.adapter().synthesize(
                request, ctl.progress, ctl.cancelled
            )
            if not temp_path.is_file():
                raise EngineError("empty_output", "Engine produced no audio file")
            if ctl.cancelled():
                raise JobCanceled()
            take_id = new_id()
            final_path = self.settings.takes_dir / f"{take_id}.wav"
            os.replace(temp_path, final_path)
            manifest = read_manifest(self.settings, model_id) or {}
            request_snapshot = {
                "text": payload["text"],
                "instruction": payload["instruction"],
                "language": payload["language"],
                "seed": int(payload["seed"]),
                "rate": float(payload["rate"]),
                "model_id": model_id,
                "mode": "design",
            }
            result_snapshot = {
                "sample_rate": result.sample_rate,
                "duration": result.duration,
                "chunks": result.chunks,
                "warnings": result.warnings,
                "engine": result.engine_info,
                "model_revision": manifest.get("revision"),
            }
            self.db.execute(
                "INSERT INTO takes (id,project_id,segment_id,voice_id,kind,path,created_at,"
                "duration,sample_rate,seed,model_id,input_hash,request_json,result_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    take_id,
                    None,
                    None,
                    None,
                    "design",
                    str(final_path),
                    utcnow_iso(),
                    result.duration,
                    result.sample_rate,
                    int(payload["seed"]),
                    model_id,
                    "",
                    json_dumps(request_snapshot),
                    json_dumps(result_snapshot),
                ),
            )
            voice_id = None
            if payload.get("save_as_voice"):
                try:
                    voice = voices.create_synthetic_voice(
                        self.settings,
                        self.db,
                        final_path,
                        text=payload["text"],
                        language=payload["language"],
                        name=payload.get("name") or "Designed voice",
                    )
                except Exception:
                    self.db.execute("DELETE FROM takes WHERE id = ?", (take_id,))
                    _unlink(final_path)
                    raise
                voice_id = voice["id"]
                self.db.execute("UPDATE takes SET voice_id = ? WHERE id = ?", (voice_id, take_id))
            ctl.set_result({"take_id": take_id, "voice_id": voice_id})
        except (JobCanceled, EngineCancelled):
            _unlink(temp_path)
            raise JobCanceled()
        finally:
            _unlink(temp_path)

    # ---- download ------------------------------------------------------
    def download(self, ctl: JobControls, payload: dict) -> None:
        model_id = payload["model_id"]
        spec = MODELS_BY_ID.get(model_id)
        if spec is None:
            raise ApiError(422, "invalid_model", "Unknown model")
        required = int(spec.size_gb * 1024**3 * 1.35) + 2 * 1024**3
        free = shutil.disk_usage(self.settings.home).free
        if free < required:
            raise EngineError(
                "insufficient_disk",
                f"Not enough free disk space: need about {required / 1024**3:.1f} GB, have {free / 1024**3:.1f} GB",
            )
        manifest = download_model(self.settings, spec, ctl.progress, ctl.cancelled)
        ctl.set_result(
            {
                "model_id": model_id,
                "revision": manifest["revision"],
                "installed": True,
                "files": len(manifest["files"]),
            }
        )

    # ---- unload --------------------------------------------------------
    def unload(self, ctl: JobControls, payload: dict) -> None:
        self.manager.unload()
        ctl.set_result({"ok": True})

    # ---- export --------------------------------------------------------
    def export(self, ctl: JobControls, payload: dict) -> None:
        ctl.check()
        ctl.progress(0.05, "mixing")
        info = export_service.render_export(self.settings, payload)
        export_service.record_export(self.db, payload["project_id"], ctl.job_id, info)
        ctl.progress(1.0, "done")
        ctl.set_result(
            {"export_id": info["id"], "format": info["format"], "filename": info["filename"]}
        )


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
