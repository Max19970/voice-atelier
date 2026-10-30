"""Persisted serial job queue: one worker, cooperative cancel, honest progress."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable

from .config import Settings
from .db import Database
from .errors import ApiError, EngineCancelled, EngineError, JobCanceled
from .util import json_dumps, json_loads, utcnow_iso

log = logging.getLogger("voice_atelier.jobs")

TERMINAL_STATES = {"done", "failed", "canceled"}
Handler = Callable[["JobControls", dict], None]


class JobControls:
    def __init__(self, runner: "JobRunner", job_id: str) -> None:
        self.runner = runner
        self.job_id = job_id
        self.result: dict | None = None
        self._last_progress = 0.0
        self._last_stage = ""
        self._last_checked = 0.0
        self._last_cancel = False

    def progress(self, value: float, stage: str) -> None:
        value = max(0.0, min(1.0, float(value)))
        now = time.monotonic()
        if (
            abs(value - self._last_progress) < 0.01
            and stage == self._last_stage
            and now - getattr(self, "_last_write", 0.0) < 0.5
        ):
            return
        self._last_progress = value
        self._last_stage = stage
        self._last_write = now
        self.runner.db.execute(
            "UPDATE jobs SET progress=?, stage=? WHERE id=?", (value, stage, self.job_id)
        )

    def cancelled(self) -> bool:
        now = time.monotonic()
        if now - self._last_checked < 0.5:
            return self._last_cancel
        self._last_checked = now
        row = self.runner.db.fetchone(
            "SELECT cancel_requested FROM jobs WHERE id = ?", (self.job_id,)
        )
        self._last_cancel = bool(row and row["cancel_requested"])
        return self._last_cancel

    def check(self) -> None:
        if self.cancelled():
            raise JobCanceled()

    def set_result(self, result: dict) -> None:
        self.result = result


class JobRunner:
    def __init__(self, settings: Settings, db: Database, handlers: dict[str, Handler]) -> None:
        self.settings = settings
        self.db = db
        self.handlers = handlers
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._reconcile()
        for row in self.db.fetchall(
            "SELECT id FROM jobs WHERE state='queued' ORDER BY created_at ASC"
        ):
            self._queue.put(row["id"])
        self._thread = threading.Thread(target=self._loop, name="voice-atelier-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _reconcile(self) -> None:
        self.db.execute(
            "UPDATE jobs SET state='failed', stage='interrupted', finished_at=?, "
            "error_json=? WHERE state='running'",
            (
                utcnow_iso(),
                json.dumps(
                    {"error": {"code": "interrupted", "message": "Server restarted while this job ran"}}
                ),
            ),
        )

    # ---- public API ----------------------------------------------------
    def enqueue(self, kind: str, payload: dict) -> dict:
        if kind not in self.handlers:
            raise ApiError(400, "invalid_request", f"Unknown job kind: {kind}")
        job_id = uuid.uuid4().hex
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
            if int(active) >= self.settings.max_active_jobs:
                raise ApiError(
                    429,
                    "queue_full",
                    f"At most {self.settings.max_active_jobs} active jobs are allowed",
                )
            conn.execute(
                "INSERT INTO jobs (id,kind,state,progress,stage,cancel_requested,payload_json,"
                "created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    kind,
                    "queued",
                    0.0,
                    "queued",
                    0,
                    json_dumps(payload),
                    utcnow_iso(),
                ),
            )
            conn.commit()
        self._queue.put(job_id)
        return self.get(job_id)

    def get(self, job_id: str) -> dict:
        row = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise ApiError(404, "not_found", "Job not found")
        return job_public(row)

    def list(self, limit: int | None = None) -> list[dict]:
        limit = limit or self.settings.job_history_limit
        rows = self.db.fetchall(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [job_public(row) for row in rows]

    def cancel(self, job_id: str) -> dict:
        row = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise ApiError(404, "not_found", "Job not found")
        if row["state"] in TERMINAL_STATES:
            return job_public(row)
        # CAS: only a still-queued, not-yet-starting job may jump to canceled.
        changed = self.db.execute(
            "UPDATE jobs SET state='canceled', cancel_requested=1, stage='canceled', "
            "finished_at=? WHERE id=? AND state='queued' AND cancel_requested=0",
            (utcnow_iso(), job_id),
        )
        if changed == 0:
            self.db.execute(
                "UPDATE jobs SET cancel_requested=1 WHERE id=? AND state='running'", (job_id,)
            )
        return self.get(job_id)

    # ---- worker --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id is None:
                break
            try:
                self._execute(job_id)
            except Exception:  # pragma: no cover - defensive
                log.exception("job %s crashed outside handler", job_id)
            finally:
                self._queue.task_done()

    def _execute(self, job_id: str) -> None:
        row = self.db.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None or row["state"] != "queued":
            return  # canceled before start, or already handled
        # CAS start: a concurrent cancel that already flipped the row to
        # canceled must never be resurrected into running.
        started = self.db.execute(
            "UPDATE jobs SET state='running', started_at=?, stage='starting' "
            "WHERE id=? AND state='queued' AND cancel_requested=0",
            (utcnow_iso(), job_id),
        )
        if started != 1:
            return
        controls = JobControls(self, job_id)
        payload = json_loads(row["payload_json"], {}) or {}
        try:
            self.handlers[row["kind"]](controls, payload)
        except JobCanceled:
            self._finish(job_id, "canceled", None)
        except EngineCancelled:
            self._finish(job_id, "canceled", None)
        except ApiError as exc:
            self._finish(job_id, "failed", {"code": exc.code, "message": exc.message})
        except EngineError as exc:
            self._finish(job_id, "failed", {"code": exc.code, "message": exc.message})
        except Exception:  # pragma: no cover - defensive
            log.exception("job %s failed", job_id)
            self._finish(job_id, "failed", {"code": "internal", "message": "Internal job error"})
        else:
            if controls.cancelled():
                self._finish(job_id, "canceled", None)
            else:
                self._finish(job_id, "done", None, controls.result or {})

    def _finish(
        self, job_id: str, state: str, error: dict | None, result: dict | None = None
    ) -> None:
        current = self.db.fetchone("SELECT state FROM jobs WHERE id = ?", (job_id,))
        if current is None or current["state"] in TERMINAL_STATES:
            return
        self.db.execute(
            "UPDATE jobs SET state=?, stage=?, finished_at=?, error_json=?, result_json=? "
            "WHERE id=?",
            (
                state,
                state,
                utcnow_iso(),
                json.dumps({"error": error}) if error else None,
                json.dumps(result) if result is not None else None,
                job_id,
            ),
        )


def job_public(row: dict) -> dict:
    error = None
    if row.get("error_json"):
        parsed = json_loads(row["error_json"], {}) or {}
        error = parsed.get("error")
    return {
        "id": row["id"],
        "kind": row["kind"],
        "state": row["state"],
        "progress": round(float(row["progress"] or 0.0), 3),
        "stage": row["stage"] or "",
        "cancel_requested": bool(row["cancel_requested"]),
        "error": error,
        "result": json_loads(row["result_json"]) if row.get("result_json") else None,
        "created_at": row["created_at"],
    }
