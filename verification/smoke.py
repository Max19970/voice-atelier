"""Real integration smoke driver for Voice Atelier.

This script starts the *real* project server (``python -m studio --port 8765``) as a
subprocess, talks to it over loopback HTTP, and drives actual synthesis through the
public API. There is no test adapter, no fake engine and no synthesized silent audio
anywhere in this file: if the engine cannot run, the smoke FAILS or stays BLOCKED.

Ownership: this file (and ``verification/browser_smoke.py``) belong to the verification
agent. Nothing outside ``verification/`` is written. User recordings under ``data/voices``
are never modified; the driver only creates its own synthetic design voice, its own
project and its own export artifacts.

Honesty rules baked in:
  * Weights are never downloaded unless ``--download-approved`` is passed explicitly.
  * Audio is only checked for decodability, length and non-silence. This is NOT a
    quality or speaker-similarity judgement, and no audition is performed.
  * Every step result is written to ``verification/artifacts/smoke-report.json``.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import wave
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# Contract constants (kept in sync with SPEC.md / studio/engine/catalog.py)
# --------------------------------------------------------------------------------------

CLONE_MODEL = "qwen-base-1.7b"
DESIGN_MODEL = "qwen-design-1.7b"
DOWNLOAD_MODELS = (CLONE_MODEL, DESIGN_MODEL)

# ~97 chars of neutral Russian: roughly 6-8 s of speech at a normal rate.
DESIGN_TEXT = (
    "Это синтетический тестовый голос для проверки студии. "
    "Он не копирует ничей реальный голос."
)
# Describes a NEW identity. Design never claims to preserve an uploaded voice.
DESIGN_INSTRUCTION = (
    "Спокойный нейтральный женский голос, средний темп речи, ясная дикция, "
    "без выраженных эмоций."
)
CLONE_LINES = (
    "Первая проверочная строка для клонирования голоса в локальной студии.",
    "Вторая проверочная строка подтверждает, что очередь заданий работает последовательно.",
)
SEED = 42

TOKEN_META_RE = re.compile(
    r"<meta\b[^>]*>", re.IGNORECASE
)
ATTR_RE = re.compile(r"""([a-zA-Z:_-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")


# --------------------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------------------


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wav_info(source: Path | bytes) -> dict[str, Any]:
    """Decode a PCM WAV (file or bytes) and report objective properties only.

    Raises ValueError when the payload is not a readable PCM WAV, which is itself a
    useful failure signal (a truncated or fake file must not pass).
    """
    stream = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else None
    handle = stream if stream is not None else source.open("rb")
    try:
        with wave.open(handle, "rb") as reader:
            channels = reader.getnchannels()
            sampwidth = reader.getsampwidth()
            framerate = reader.getframerate()
            frames = reader.getnframes()
            raw = reader.readframes(frames)
    finally:
        if stream is None:
            handle.close()

    if framerate <= 0 or frames <= 0:
        raise ValueError("wav has no frames")

    if sampwidth == 2:
        samples = array.array("h")
        limit = 32767.0
    elif sampwidth == 1:
        samples = array.array("b")
        limit = 127.0
    elif sampwidth == 4:
        samples = array.array("i")
        limit = 2147483647.0
    else:
        raise ValueError(f"unsupported sample width: {sampwidth}")
    samples.frombytes(raw[: (len(raw) // sampwidth) * sampwidth])
    if sys.byteorder == "big":
        samples.byteswap()

    peak = max((abs(value) for value in samples), default=0)
    clipped = sum(1 for value in samples if abs(value) >= limit * 0.999)
    total = len(samples) or 1

    return {
        "channels": channels,
        "sample_width": sampwidth,
        "sample_rate": framerate,
        "frames": frames,
        "duration": round(frames / float(framerate), 3),
        "peak": int(peak),
        "peak_normalized": round(peak / limit, 4),
        "nonzero": peak > 0,
        "clipped_fraction": round(clipped / total, 6),
        "bytes": len(raw),
    }


def port_is_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def tail(path: Path, limit: int = 40) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-limit:])


def extract_token(html: str) -> str | None:
    """Pull the per-launch token out of the escaped ``studio-token`` meta tag."""
    for tag in TOKEN_META_RE.findall(html):
        attrs = {}
        for name, quoted, single in ATTR_RE.findall(tag):
            attrs[name.lower()] = quoted or single
        if attrs.get("name") == "studio-token" and attrs.get("content"):
            return attrs["content"]
    return None


# --------------------------------------------------------------------------------------
# HTTP client (httpx when available, stdlib urllib otherwise)
# --------------------------------------------------------------------------------------


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    content: bytes
    json: Any = None
    text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Client:
    """Minimal HTTP client bound to one loopback base URL with a fixed token."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 60.0,
        recorder: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.recorder = recorder
        self._httpx = None
        try:
            import httpx  # noqa: F401  (present in the project runtime)

            self._httpx = httpx.Client(base_url=self.base_url, timeout=timeout)
        except Exception:
            self._httpx = None

    def close(self) -> None:
        if self._httpx is not None:
            try:
                self._httpx.close()
            except Exception:
                pass
            self._httpx = None

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        auth: bool = True,
        timeout: float | None = None,
    ) -> Response:
        send_headers = {"Accept": "application/json"}
        if auth:
            send_headers["X-Studio-Token"] = self.token
        if headers:
            send_headers.update(headers)
        payload = None
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
            send_headers["Content-Type"] = "application/json"

        if self._httpx is not None:
            raw = self._httpx.request(
                method,
                path,
                content=payload,
                headers=send_headers,
                timeout=timeout or self.timeout,
            )
            response = Response(
                status=raw.status_code,
                headers={key.lower(): value for key, value in raw.headers.items()},
                content=raw.content,
            )
        else:  # pragma: no cover - fallback path, exercised only without httpx
            import urllib.error
            import urllib.request

            request = urllib.request.Request(
                self.base_url + path, data=payload, method=method, headers=send_headers
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout or self.timeout) as opened:
                    response = Response(
                        status=opened.status,
                        headers={key.lower(): value for key, value in opened.headers.items()},
                        content=opened.read(),
                    )
            except urllib.error.HTTPError as error:
                response = Response(
                    status=error.code,
                    headers={key.lower(): value for key, value in error.headers.items()},
                    content=error.read(),
                )

        content_type = response.headers.get("content-type", "")
        if "json" in content_type or response.content[:1] in (b"{", b"["):
            try:
                response.json = json.loads(response.content.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                response.json = None
        if response.json is None:
            response.text = response.content.decode("utf-8", errors="replace")
        if self.recorder is not None:
            try:
                self.recorder(method, path, response)
            except Exception:
                pass
        return response

    def get(self, path: str, **kwargs: Any) -> Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Response:
        return self.request("DELETE", path, **kwargs)


# --------------------------------------------------------------------------------------
# Driver errors / report
# --------------------------------------------------------------------------------------


class DriverError(RuntimeError):
    """A fatal, non-recoverable condition (port busy, server died, no token)."""


class StepError(RuntimeError):
    """A single smoke step did not meet the contract."""


@dataclass
class Step:
    name: str
    status: str  # PASS | FAIL | BLOCKED | SKIP
    started_at: str
    elapsed_s: float
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Report:
    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.environment: dict[str, Any] = {}
        self.gpu: dict[str, Any] = {}
        self.artifacts: list[dict[str, Any]] = []
        self.limitations: list[str] = []
        self.fatal: str | None = None

    def add(
        self,
        name: str,
        status: str,
        started: float,
        *,
        detail: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> Step:
        step = Step(
            name=name,
            status=status,
            started_at=utcnow(),
            elapsed_s=round(time.monotonic() - started, 3),
            detail=detail or {},
            error=error,
        )
        self.steps.append(step)
        marker = {"PASS": "PASS", "FAIL": "FAIL", "BLOCKED": "BLOCK", "SKIP": "skip"}[status]
        line = f"[{marker}] {name} ({step.elapsed_s}s)"
        if error:
            line += f" :: {error}"
        print(line, flush=True)
        return step

    def artifact(self, path: Path, note: str) -> None:
        entry = {"path": str(path), "bytes": path.stat().st_size if path.is_file() else 0, "note": note}
        if path.is_file():
            entry["sha256"] = sha256_file(path)
        self.artifacts.append(entry)

    def counts(self) -> dict[str, int]:
        counts = {"PASS": 0, "FAIL": 0, "BLOCKED": 0, "SKIP": 0}
        for step in self.steps:
            counts[step.status] = counts.get(step.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "generated_at": utcnow(),
            "driver": "verification/smoke.py",
            "contract": "SPEC.md v1",
            "environment": self.environment,
            "gpu": self.gpu,
            "summary": {
                **counts,
                "audio_verified": any(
                    step.name.startswith("audio:") and step.status == "PASS" for step in self.steps
                ),
                "exit_hint": "PASS" if counts["FAIL"] == 0 and counts["BLOCKED"] == 0 else "ATTENTION",
            },
            "fatal": self.fatal,
            "steps": [
                {
                    "name": step.name,
                    "status": step.status,
                    "started_at": step.started_at,
                    "elapsed_s": step.elapsed_s,
                    "detail": step.detail,
                    "error": step.error,
                }
                for step in self.steps
            ],
            "artifacts": self.artifacts,
            "limitations": self.limitations,
        }


# --------------------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------------------


class Driver:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Path(args.project_root).resolve()
        self.verification_dir = self.root / "verification"
        self.artifacts_dir = Path(args.artifacts_dir).resolve()
        self.report = Report()
        self.client: Client | None = None
        self.server: subprocess.Popen[bytes] | None = None
        self.server_log_handle = None
        self.server_log = self.verification_dir / "server.log"
        self.token = args.token or ""
        self.base_url = f"http://127.0.0.1:{args.port}"
        self.exit_code = 0
        self.models_ready = False
        self.calls: list[dict[str, Any]] = []
        self._started_monotonic = 0.0
        self.pytest_failed = False
        self.external_server = False
        self.browser_result: dict[str, Any] | None = None
        self.voice_id: str | None = None
        self.design_take_id: str | None = None
        self.project_id: str | None = None
        self.project_segments: list[dict[str, Any]] = []

    # -- preflight ---------------------------------------------------------------------
    def preflight(self) -> None:
        for required in ("studio/__main__.py", "studio/app.py", "SPEC.md"):
            if not (self.root / required).is_file():
                raise DriverError(f"missing required project file: {required} (is --project-root right?)")
        self.verification_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._started_monotonic = time.monotonic()
        self.report.environment = {
            "started_at": utcnow(),
            "project_root": str(self.root),
            "base_url": self.base_url,
            "mode": "attach" if self.args.no_server else "own-server",
            "python": self.args.python,
            "driver_python": sys.executable,
            "port": self.args.port,
            "download_approved": bool(self.args.download_approved),
            "data_home": str(self.args.home) if self.args.home else "<project data/>",
            "artifacts": str(self.artifacts_dir),
        }
        self.report.limitations = [
            "Audio checks are objective only: PCM decodes, duration > 1 s, non-zero peak. "
            "No quality, naturalness or speaker-similarity judgement was performed.",
            "No audition was performed; nobody listened to these files.",
            "Only one seed per line and a single design instruction were exercised.",
            "Model quality claims in docs come from vendor benchmarks, not this run.",
            "The design model creates a NEW synthetic identity; it is not a clone of any real voice.",
            "Only this driver's own fixed-name artifacts are written/overwritten under "
            "verification/artifacts; no existing voice or recording is read or modified.",
            "Each run creates a new synthetic voice, project and takes under data/ (new UUIDs); "
            "nothing is deleted there, so repeated runs accumulate test entities.",
        ]

    # -- pytest (optional, informational) ----------------------------------------------
    def run_pytest(self) -> None:
        started = time.monotonic()
        log_path = self.verification_dir / "pytest.log"
        cmd = [self.args.python, "-m", "pytest", "tests", "-q"]
        try:
            with log_path.open("w", encoding="utf-8") as handle:
                handle.write(f"$ {' '.join(cmd)}\n\n")
                handle.flush()
                completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
                    cmd,
                    cwd=str(self.root),
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=self.args.pytest_timeout,
                )
            self.report.artifact(log_path, "pytest output")
            ok = completed.returncode == 0
            self.report.add(
                "unit:pytest",
                "PASS" if ok else "FAIL",
                started,
                detail={"returncode": completed.returncode, "log": str(log_path)},
                error=None if ok else f"pytest exited {completed.returncode}; see {log_path.name}",
            )
            if not ok:
                self.pytest_failed = True
                self.exit_code = max(self.exit_code, 1)
        except FileNotFoundError:
            self.report.add("unit:pytest", "SKIP", started, detail={"reason": "python not found"})
        except subprocess.TimeoutExpired:
            self.pytest_failed = True
            self.report.add(
                "unit:pytest",
                "FAIL",
                started,
                error=f"pytest exceeded {self.args.pytest_timeout}s",
            )
            self.exit_code = max(self.exit_code, 1)

    # -- server lifecycle --------------------------------------------------------------
    def start_server(self) -> None:
        if self.args.no_server:
            if not port_is_open("127.0.0.1", self.args.port):
                raise DriverError(
                    f"--no-server was given but nothing is listening on 127.0.0.1:{self.args.port}"
                )
            self.external_server = True
            self.report.environment["server_owner"] = "external (never terminated by this driver)"
            return

        if port_is_open("127.0.0.1", self.args.port):
            # Health JSON plus the token meta are NOT proof of identity: any local process
            # can mimic both. So there is deliberately no auto-attach, and an unverified
            # listener is never touched.
            raise DriverError(
                f"port {self.args.port} is already in use. This driver will not attach to or "
                "kill an unverified listener. Stop that process or choose another --port. "
                "(--no-server exists only for a server you already trust.)"
            )

        cmd = [self.args.python, "-m", "studio", "--port", str(self.args.port)]
        if self.args.home:
            cmd.extend(["--home", str(self.args.home)])
        if self.args.log_level:
            cmd.extend(["--log-level", self.args.log_level])

        self.server_log.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately a file handle, never a pipe: the child must not block on a full
        # pipe, and the full log has to survive for post-mortem review.
        self.server_log_handle = self.server_log.open("w", encoding="utf-8", errors="replace")
        self.server_log_handle.write(f"$ {' '.join(cmd)}\ncwd={self.root}\n\n")
        self.server_log_handle.flush()

        env = os.environ.copy()
        if self.args.home:
            env["VOICE_ATELIER_HOME"] = str(self.args.home)
        else:
            # Default to the real project data/ directory even if the shell exports a home.
            env.pop("VOICE_ATELIER_HOME", None)
        env["PYTHONUNBUFFERED"] = "1"

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        self.server = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            cmd,
            cwd=str(self.root),
            stdout=self.server_log_handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            creationflags=creationflags,
        )
        self.report.environment["server_pid"] = self.server.pid
        self.report.environment["server_command"] = cmd

    def wait_health(self) -> None:
        started = time.monotonic()
        deadline = started + self.args.boot_timeout
        version = None
        probe = Client(self.base_url, token="", timeout=5)
        try:
            while time.monotonic() < deadline:
                if self.server is not None and self.server.poll() is not None:
                    raise DriverError(
                        f"server exited with code {self.server.returncode} before becoming healthy.\n"
                        f"--- {self.server_log.name} tail ---\n{tail(self.server_log)}"
                    )
                try:
                    response = probe.get("/api/health", auth=False)
                    if response.ok and isinstance(response.json, dict) and response.json.get("ok"):
                        version = response.json.get("version")
                        break
                except Exception:
                    pass
                time.sleep(0.4)
        finally:
            probe.close()
        if version is None:
            raise DriverError(
                f"health check did not pass within {self.args.boot_timeout}s.\n"
                f"--- {self.server_log.name} tail ---\n{tail(self.server_log)}"
            )
        self.report.add(
            "server:health",
            "PASS",
            started,
            detail={"version": version, "url": self.base_url, "log": str(self.server_log)},
        )

    def fetch_token(self) -> None:
        started = time.monotonic()
        if self.token:
            self.report.add("server:token", "SKIP", started, detail={"reason": "token supplied via --token"})
            return
        anonymous = Client(self.base_url, token="", timeout=15)
        try:
            response = anonymous.get("/", auth=False)
        finally:
            anonymous.close()
        if not response.ok:
            raise DriverError(f"could not load index page for token: HTTP {response.status}")
        token = extract_token(response.text)
        if not token:
            raise DriverError(
                "no <meta name='studio-token'> found in index HTML; cannot authenticate"
            )
        self.token = token
        self.report.add(
            "server:token",
            "PASS",
            started,
            detail={"length": len(token), "source": "index meta"},
        )

    def shutdown(self) -> None:
        if self.client is not None:
            self.client.close()
            self.client = None
        if self.server is not None:
            pid = self.server.pid
            try:
                if self.server.poll() is None:
                    self.server.terminate()
                    try:
                        self.server.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        self.server.kill()
                        self.server.wait(timeout=10)
            except Exception:
                pass
            finally:
                print(f"[info] terminated own server pid {pid}", flush=True)
                self.server = None
        if self.server_log_handle is not None:
            try:
                self.server_log_handle.flush()
                self.server_log_handle.close()
            except Exception:
                pass
            self.server_log_handle = None

    # -- helpers -----------------------------------------------------------------------
    def _record_call(self, method: str, path: str, response: Response) -> None:
        """Keep an explicit endpoint/result ledger for the JSON report."""
        self.calls.append(
            {
                "method": method,
                "path": path,
                "status": response.status,
                "bytes": len(response.content),
                "at_s": round(time.monotonic() - self._started_monotonic, 2)
                if self._started_monotonic
                else None,
            }
        )

    def client_or_fail(self) -> Client:
        if self.client is None:
            raise DriverError("HTTP client is not initialised")
        return self.client

    def expect(self, condition: bool, message: str) -> None:
        if not condition:
            raise StepError(message)

    def expect_status(self, response: Response, *statuses: int) -> None:
        if response.status not in statuses:
            raise StepError(
                f"unexpected HTTP {response.status} (wanted {statuses}): "
                f"{(response.text or '').strip()[:400] or response.json}"
            )

    def wait_job(self, job: dict[str, Any], *, timeout: float, label: str) -> dict[str, Any]:
        """Poll a job honestly: status of progress only, never a fabricated percentage."""
        client = self.client_or_fail()
        job_id = job.get("id")
        if not job_id:
            raise StepError(f"{label}: enqueue did not return a job id: {job}")
        started = time.monotonic()
        deadline = started + timeout
        last_stage = ""
        last_beat = started
        state = job.get("state", "queued")
        current = job
        while time.monotonic() < deadline:
            response = client.get(f"/api/jobs/{job_id}")
            self.expect_status(response, 200)
            current = response.json or {}
            state = current.get("state", "")
            stage = current.get("stage") or ""
            if stage != last_stage:
                progress = current.get("progress")
                shown = f"{float(progress):.0%}" if isinstance(progress, (int, float)) else "?"
                print(f"  · {label} [{job_id[:8]}] {state} {shown} {stage}", flush=True)
                last_stage = stage
                last_beat = time.monotonic()
            elif time.monotonic() - last_beat >= 60:
                print(
                    f"  · {label} [{job_id[:8]}] still {state} "
                    f"({int(time.monotonic() - started)}s elapsed) {stage}",
                    flush=True,
                )
                last_beat = time.monotonic()
            if state in ("done", "failed", "canceled"):
                break
            time.sleep(self.args.poll_seconds)
        else:
            raise StepError(
                f"{label}: job {job_id} still {state} after {int(timeout)}s (stage={last_stage!r}). "
                "Server may still be working; increase --job-timeout."
            )

        if state != "done":
            error = current.get("error") or {}
            raise StepError(
                f"{label}: job ended {state}: "
                f"{error.get('code', '?')} {error.get('message', '')}".strip()
            )
        result = current.get("result") or {}
        print(f"  · {label} done in {int(time.monotonic() - started)}s", flush=True)
        return result

    def download(self, path: str, target: Path, note: str) -> dict[str, Any]:
        client = self.client_or_fail()
        response = client.get(path)
        self.expect_status(response, 200)
        self.expect(len(response.content) > 0, f"{path} returned an empty body")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(response.content)
        self.report.artifact(target, note)
        return {
            "path": str(target),
            "bytes": len(response.content),
            "content_type": response.headers.get("content-type", ""),
        }

    # -- steps -------------------------------------------------------------------------
    def step_status_and_models(self) -> None:
        started = time.monotonic()
        client = self.client_or_fail()
        response = client.get("/api/status")
        self.expect_status(response, 200)
        status = response.json or {}
        capabilities = status.get("capabilities") or {}
        self.expect(
            capabilities.get("clone_instruction") is False,
            f"contract says clone_instruction must be false, got {capabilities.get('clone_instruction')!r}",
        )
        self.expect(
            capabilities.get("design_instruction") is True,
            "contract says design_instruction must be true",
        )
        self.expect("ru" in (capabilities.get("languages") or []), "ru missing from capabilities.languages")
        rate = capabilities.get("rate") or {}
        self.expect(
            rate.get("mode") == "postprocess",
            f"rate.mode must be postprocess, got {rate.get('mode')!r}",
        )
        data_dir = (status.get("paths") or {}).get("data")
        self.expect(bool(data_dir), "status.paths.data is missing")

        models_response = client.get("/api/models")
        self.expect_status(models_response, 200)
        items = (models_response.json or {}).get("items") or []
        by_id = {item.get("id"): item for item in items}
        for model_id in DOWNLOAD_MODELS:
            self.expect(model_id in by_id, f"model {model_id} missing from /api/models")
        for item in items:
            self.expect(bool(item.get("revision")), f"model {item.get('id')} has no pinned revision")

        self.models_ready = all(bool(by_id[model_id].get("installed")) for model_id in DOWNLOAD_MODELS)
        detail = {
            "version": status.get("version"),
            "offline": status.get("offline"),
            "engine_available": status.get("engine_available"),
            "device": status.get("device"),
            "loaded_model": status.get("loaded_model"),
            "data_dir": data_dir,
            "warnings": status.get("warnings"),
            "models": [
                {
                    "id": item.get("id"),
                    "installed": item.get("installed"),
                    "status": item.get("status"),
                    "revision": item.get("revision"),
                    "size_gb": item.get("size_gb"),
                }
                for item in items
            ],
        }
        self.report.environment["data_dir"] = data_dir
        self.report.add("server:status", "PASS", started, detail=detail)

    def step_negative(self) -> None:
        started = time.monotonic()
        client = self.client_or_fail()
        checks: dict[str, Any] = {}

        unauth = client.get("/api/projects", auth=False)
        self.expect_status(unauth, 401)
        checks["unauthorized"] = {
            "status": unauth.status,
            "code": ((unauth.json or {}).get("error") or {}).get("code"),
        }

        foreign = client.post(
            "/api/projects",
            json_body={"title": "origin-probe-should-not-exist"},
            headers={"Origin": "http://evil.example"},
        )
        self.expect_status(foreign, 400)
        checks["foreign_origin"] = {
            "status": foreign.status,
            "code": ((foreign.json or {}).get("error") or {}).get("code"),
        }

        bad_host = client.get("/api/health", headers={"Host": "evil.example"}, auth=False)
        self.expect_status(bad_host, 400)
        checks["invalid_host"] = {
            "status": bad_host.status,
            "code": ((bad_host.json or {}).get("error") or {}).get("code"),
        }

        # A clone segment carrying an instruction must be rejected by validation, before
        # any job is enqueued. This is the honest guard against "instruction" on clone.
        probe = client.post("/api/projects", json_body={"title": "instruction-probe"})
        self.expect_status(probe, 201)
        probe_id = (probe.json or {}).get("id")
        self.expect(bool(probe_id), "probe project has no id")
        try:
            rejected = client.put(
                f"/api/projects/{probe_id}",
                json_body={
                    "title": "instruction-probe",
                    "segments": [
                        {
                            "text": "Проверка запрета инструкции в режиме клонирования.",
                            "mode": "clone",
                            "model_id": CLONE_MODEL,
                            "language": "ru",
                            "instruction": "говори взволнованно и шёпотом",
                            "seed": SEED,
                        }
                    ],
                },
            )
            self.expect_status(rejected, 422)
            checks["clone_instruction"] = {
                "status": rejected.status,
                "code": ((rejected.json or {}).get("error") or {}).get("code"),
            }
            self.expect(
                checks["clone_instruction"]["code"] == "clone_instruction_not_supported",
                f"unexpected clone-instruction error code: {checks['clone_instruction']['code']!r}",
            )
        finally:
            client.delete(f"/api/projects/{probe_id}")

        self.report.add("api:negative-checks", "PASS", started, detail=checks)

    def step_download(self) -> None:
        client = self.client_or_fail()
        # Weights must never be fetched when the unit suite is red: "download only
        # after tests actually pass" is enforced here, not just in the launcher.
        if (
            self.args.download_approved
            and self.pytest_failed
            and not self.args.allow_download_after_failed_tests
        ):
            self.report.add(
                "models:download",
                "BLOCKED",
                time.monotonic(),
                detail={"approved": True, "pytest_failed": True},
                error=(
                    "refusing to download weights because pytest failed; fix the tests "
                    "or pass --allow-download-after-failed-tests"
                ),
            )
            self.exit_code = max(self.exit_code, 1)
            return
        if not self.args.download_approved:
            status = client.get("/api/models")
            self.expect_status(status, 200)
            installed = {
                item.get("id"): bool(item.get("installed"))
                for item in (status.json or {}).get("items") or []
            }
            missing = [model_id for model_id in DOWNLOAD_MODELS if not installed.get(model_id)]
            self.models_ready = not missing
            if missing:
                self.report.add(
                    "models:download",
                    "BLOCKED",
                    time.monotonic(),
                    detail={"missing": missing, "approved": False},
                    error=(
                        "weights are not installed and --download-approved was not passed; "
                        "audio steps cannot run. Re-run with --download-approved (user approved)."
                    ),
                )
                self.exit_code = max(self.exit_code, 2)
            else:
                self.report.add(
                    "models:download",
                    "SKIP",
                    time.monotonic(),
                    detail={"reason": "both models already installed", "approved": False},
                )
            return

        for model_id in DOWNLOAD_MODELS:
            started = time.monotonic()
            listing = client.get("/api/models")
            self.expect_status(listing, 200)
            entry = next(
                (item for item in (listing.json or {}).get("items") or [] if item.get("id") == model_id),
                None,
            )
            self.expect(entry is not None, f"model {model_id} not in catalog")
            if entry.get("installed"):
                self.report.add(
                    f"models:download:{model_id}",
                    "SKIP",
                    started,
                    detail={"reason": "already installed", "revision": entry.get("revision")},
                )
                continue
            enqueue = client.post(
                f"/api/models/{model_id}/download", json_body={"accept_license": True}
            )
            self.expect_status(enqueue, 200)
            job = enqueue.json or {}
            result = self.wait_job(
                job, timeout=self.args.job_timeout, label=f"download {model_id}"
            )
            self.expect(bool(result.get("installed")), f"download {model_id} did not report installed")
            verify = client.get("/api/models")
            self.expect_status(verify, 200)
            entry = next(
                (item for item in (verify.json or {}).get("items") or [] if item.get("id") == model_id),
                {},
            )
            self.expect(bool(entry.get("installed")), f"model {model_id} is not installed after download")
            self.report.add(
                f"models:download:{model_id}",
                "PASS",
                started,
                detail={
                    "revision": result.get("revision"),
                    "files": result.get("files"),
                    "installed": True,
                },
            )

        final = client.get("/api/models")
        self.expect_status(final, 200)
        installed_now = {
            item.get("id"): bool(item.get("installed"))
            for item in (final.json or {}).get("items") or []
        }
        self.models_ready = all(installed_now.get(model_id) for model_id in DOWNLOAD_MODELS)

    def require_models(self) -> bool:
        return self.models_ready

    def step_design(self) -> bool:
        started = time.monotonic()
        client = self.client_or_fail()
        # The backend intentionally keeps design prompts short to avoid identity drift
        # across chunks. Keep this driver inside that policy rather than fighting it.
        self.expect(
            len(DESIGN_TEXT) <= 240,
            f"design text is {len(DESIGN_TEXT)} chars; the backend enforces a <=240 policy",
        )
        enqueue = client.post(
            "/api/design",
            json_body={
                "name": "SMOKE synthetic RU design voice",
                "text": DESIGN_TEXT,
                "instruction": DESIGN_INSTRUCTION,
                "language": "ru",
                "seed": SEED,
                "rate": 1.0,
                "save_as_voice": True,
                "consent": True,
            },
        )
        self.expect_status(enqueue, 200)
        result = self.wait_job(enqueue.json or {}, timeout=self.args.job_timeout, label="design")
        take_id = result.get("take_id")
        voice_id = result.get("voice_id")
        self.expect(bool(take_id), "design result has no take_id")
        self.expect(bool(voice_id), "design with save_as_voice=true returned no voice_id")
        self.design_take_id = take_id
        self.voice_id = voice_id

        take_audio = self.download(
            f"/api/takes/{take_id}/audio",
            self.artifacts_dir / "design-take-01.wav",
            "synthetic designed take (real engine output)",
        )
        ref_audio = self.download(
            f"/api/voices/{voice_id}/audio",
            self.artifacts_dir / "design-voice-ref-01.wav",
            "saved synthetic design voice reference",
        )

        info = wav_info(Path(take_audio["path"]))
        ref_info = wav_info(Path(ref_audio["path"]))
        self.expect(info["duration"] > 1.0, f"design take is only {info['duration']}s")
        self.expect(info["nonzero"], "design take is digital silence")
        self.expect(ref_info["nonzero"], "saved voice reference is digital silence")

        voices = client.get("/api/voices")
        self.expect_status(voices, 200)
        saved = next(
            (item for item in (voices.json or {}).get("items") or [] if item.get("id") == voice_id),
            None,
        )
        self.expect(saved is not None, "designed voice is not listed in /api/voices")
        self.expect(
            saved.get("source") == "design",
            f"designed voice source should be 'design', got {saved.get('source')!r}",
        )
        self.expect(
            saved.get("consent_basis") == "synthetic",
            f"designed voice consent_basis should be 'synthetic', got {saved.get('consent_basis')!r}",
        )
        self.expect(
            bool((saved.get("transcript") or "").strip()),
            "designed voice has no verbatim transcript",
        )
        self.expect(saved.get("language") == "ru", "designed voice language should be ru")

        self.report.add(
            "audio:design",
            "PASS",
            started,
            detail={
                "take_id": take_id,
                "voice_id": voice_id,
                "text_chars": len(DESIGN_TEXT),
                "take": info,
                "reference": ref_info,
                "voice_source": saved.get("source"),
                "consent_basis": saved.get("consent_basis"),
                "note": "new synthetic identity; not a clone of any uploaded voice",
            },
        )
        return True

    def step_clone_project(self) -> bool:
        started = time.monotonic()
        client = self.client_or_fail()
        created = client.post(
            "/api/projects", json_body={"title": f"SMOKE RU clone {datetime.now():%Y-%m-%d %H:%M}"}
        )
        self.expect_status(created, 201)
        project = created.json or {}
        project_id = project.get("id")
        self.expect(bool(project_id), "created project has no id")
        self.project_id = project_id

        segments = [
            {
                "text": line,
                "voice_id": self.voice_id,
                "mode": "clone",
                "model_id": CLONE_MODEL,
                "language": "ru",
                "instruction": "",
                "rate": 1.0,
                "pause_ms": 300,
                "seed": SEED,
            }
            for line in CLONE_LINES
        ]
        saved = client.put(
            f"/api/projects/{project_id}",
            json_body={"title": project.get("title"), "segments": segments},
        )
        self.expect_status(saved, 200)
        saved_project = saved.json or {}
        self.expect(len(saved_project.get("segments") or []) == len(CLONE_LINES), "segments not saved")
        segment_ids = [segment.get("id") for segment in saved_project.get("segments") or []]
        self.expect(all(segment_ids), "saved segments have no ids")

        enqueue = client.post(f"/api/projects/{project_id}/generate", json_body={})
        self.expect_status(enqueue, 200)
        result = self.wait_job(
            enqueue.json or {}, timeout=self.args.job_timeout, label="generate 2 clone segments"
        )
        self.expect(
            len(result.get("generated") or []) == len(CLONE_LINES),
            f"generated {len(result.get('generated') or [])} takes, wanted {len(CLONE_LINES)}: {result}",
        )
        self.expect(not (result.get("failed") or []), f"generate reported failures: {result.get('failed')}")

        refreshed = client.get(f"/api/projects/{project_id}")
        self.expect_status(refreshed, 200)
        project_after = refreshed.json or {}
        self.project_segments = project_after.get("segments") or []
        self.expect(len(self.project_segments) == len(CLONE_LINES), "project lost segments after generation")

        take_details = []
        for index, segment in enumerate(self.project_segments, start=1):
            takes = segment.get("takes") or []
            self.expect(bool(takes), f"segment {index} has no takes after generation")
            self.expect(
                bool(segment.get("selected_take_id")),
                f"segment {index} has no selected take after generation",
            )
            selected = next(
                (take for take in takes if take.get("id") == segment.get("selected_take_id")), None
            )
            self.expect(selected is not None, f"segment {index} selected take is not in takes[]")
            self.expect(not selected.get("stale"), f"segment {index} take is stale right after generation")
            self.expect(selected.get("model_id") == CLONE_MODEL, "take used a different model")
            audio = self.download(
                f"/api/takes/{selected['id']}/audio",
                self.artifacts_dir / f"clone-seg-{index:02d}.wav",
                f"cloned segment {index} (real engine output)",
            )
            info = wav_info(Path(audio["path"]))
            self.expect(info["duration"] > 1.0, f"clone take {index} is only {info['duration']}s")
            self.expect(info["nonzero"], f"clone take {index} is digital silence")
            take_details.append(
                {
                    "segment_index": index,
                    "take_id": selected["id"],
                    "duration": selected.get("duration"),
                    "seed": selected.get("seed"),
                    "model_id": selected.get("model_id"),
                    "stale": selected.get("stale"),
                    "wav": info,
                }
            )

        self.report.add(
            "audio:clone",
            "PASS",
            started,
            detail={
                "project_id": project_id,
                "voice_id": self.voice_id,
                "model_id": CLONE_MODEL,
                "lines": list(CLONE_LINES),
                "takes": take_details,
            },
        )
        return True

    def step_export(self) -> bool:
        started = time.monotonic()
        client = self.client_or_fail()
        project_id = self.project_id
        if not project_id:
            raise StepError("no project to export")

        results: dict[str, Any] = {}
        for fmt in ("zip", "wav"):
            enqueue = client.post(
                f"/api/projects/{project_id}/export",
                json_body={"format": fmt, "normalize": False, "allow_stale": False},
            )
            self.expect_status(enqueue, 200)
            job_result = self.wait_job(enqueue.json or {}, timeout=self.args.export_timeout, label=f"export {fmt}")
            export_id = job_result.get("export_id")
            self.expect(bool(export_id), f"export {fmt} returned no export_id")
            target = self.artifacts_dir / ("export-zip-01.zip" if fmt == "zip" else "export-mixed-01.wav")
            downloaded = self.download(
                f"/api/exports/{export_id}/download", target, f"{fmt} export download"
            )
            results[fmt] = {
                "export_id": export_id,
                "job": job_result,
                "download": downloaded,
                "sha256": sha256_file(target),
            }

        mixed = wav_info(Path(results["wav"]["download"]["path"]))
        self.expect(mixed["duration"] > 1.0, f"mixed export is only {mixed['duration']}s")
        self.expect(mixed["nonzero"], "mixed export is digital silence")
        self.expect(
            mixed["sample_rate"] == 24000,
            f"mixed export sample rate is {mixed['sample_rate']}, contract says 24000",
        )
        self.expect(mixed["channels"] == 1, f"mixed export has {mixed['channels']} channels, wanted mono")

        zip_path = Path(results["zip"]["download"]["path"])
        zip_checks = self.verify_zip(zip_path)
        results["zip"]["checks"] = zip_checks

        self.report.add(
            "export:zip+wav",
            "PASS",
            started,
            detail={
                "wav": {**results["wav"], "wav": mixed},
                "zip": results["zip"],
                "manifest": zip_checks["manifest_summary"],
            },
        )
        return True

    def verify_zip(self, path: Path) -> dict[str, Any]:
        self.expect(zipfile.is_zipfile(path), "export zip is not a valid zip archive")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            for required in ("mixed.wav", "manifest.json", "script.txt"):
                self.expect(required in names, f"zip is missing {required}")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

            self.expect(manifest.get("sample_rate") == 24000, "manifest sample_rate must be 24000")
            self.expect(manifest.get("normalize") is False, "manifest.normalize must be false for this run")
            segments = manifest.get("segments") or []
            self.expect(len(segments) == len(CLONE_LINES), f"manifest has {len(segments)} segments")
            mixed_sha = sha256_bytes(archive.read("mixed.wav"))
            outputs = manifest.get("outputs") or {}
            declared = (outputs.get("mixed") or {}).get("sha256")
            self.expect(declared == mixed_sha, "manifest mixed.wav sha256 does not match the archive")

            segment_checks = []
            for item in segments:
                self.expect(item.get("stale_at_export") is False, "manifest marks a segment stale at export")
                self.expect(item.get("model_id") == CLONE_MODEL, "manifest model_id mismatch")
                self.expect(bool(item.get("model_revision")), "manifest lacks model_revision")
                self.expect(item.get("language") == "ru", "manifest language is not ru")
                self.expect(item.get("instruction") == "", "clone segment must export empty instruction")
                self.expect(item.get("seed") == SEED, "manifest seed mismatch")
                self.expect(item.get("rate") == 1.0, "manifest rate mismatch")
                self.expect(item.get("pause_ms") == 300, "manifest pause_ms mismatch")
                voice = item.get("voice") or {}
                self.expect(voice.get("consent_basis") == "synthetic", "manifest voice consent basis mismatch")
                self.expect(bool(voice.get("ref_sha256")), "manifest lacks reference sha256")
                member = item.get("file")
                self.expect(bool(member) and member in names, f"zip is missing segment file {member!r}")
                payload = archive.read(member)
                self.expect(
                    item.get("take_sha256") == sha256_bytes(payload),
                    f"segment {member} sha256 does not match the manifest",
                )
                info = wav_info(payload)
                self.expect(info["duration"] > 1.0, f"{member} is only {info['duration']}s")
                self.expect(info["nonzero"], f"{member} is digital silence")
                segment_checks.append(
                    {
                        "file": member,
                        "duration": info["duration"],
                        "sample_rate": info["sample_rate"],
                        "stale_at_export": item.get("stale_at_export"),
                        "seed": item.get("seed"),
                        "take_sha256": item.get("take_sha256"),
                    }
                )

            mixed_info = wav_info(archive.read("mixed.wav"))
            self.expect(mixed_info["duration"] > 1.0, "zip mixed.wav is too short")
            self.expect(mixed_info["nonzero"], "zip mixed.wav is digital silence")
            # Mixed duration must exceed the longest single take thanks to pauses.
            durations = [entry["duration"] for entry in segment_checks]
            self.expect(
                mixed_info["duration"] >= max(durations),
                "mixed.wav is shorter than its longest segment",
            )

            script = archive.read("script.txt").decode("utf-8")
            for line in CLONE_LINES:
                self.expect(line in script, "script.txt does not contain a generated line")

            return {
                "entries": names,
                "mixed": mixed_info,
                "segments": segment_checks,
                "manifest_summary": {
                    "sample_rate": manifest.get("sample_rate"),
                    "normalize": manifest.get("normalize"),
                    "segments": len(segments),
                    "model_ids": sorted({item.get("model_id") for item in segments}),
                    "model_revisions": sorted({item.get("model_revision") for item in segments}),
                    "all_fresh": all(item.get("stale_at_export") is False for item in segments),
                    "notes": manifest.get("notes"),
                },
            }

    def step_persistence(self) -> None:
        started = time.monotonic()
        client = self.client_or_fail()
        project_id = self.project_id
        if not project_id:
            raise StepError("no project to re-read")

        again = client.get(f"/api/projects/{project_id}")
        self.expect_status(again, 200)
        project = again.json or {}
        segments = project.get("segments") or []
        self.expect(len(segments) == len(CLONE_LINES), "project segments changed between reads")
        takes_now = sum(len(segment.get("takes") or []) for segment in segments)
        self.expect(takes_now >= len(CLONE_LINES), f"project lost takes between reads ({takes_now})")
        self.expect(
            all(segment.get("selected_take_id") for segment in segments),
            "selected takes were not persisted",
        )
        for segment in segments:
            for take in segment.get("takes") or []:
                self.expect(not take.get("stale"), "persisted take reports stale after re-read")

        listing = client.get("/api/projects")
        self.expect_status(listing, 200)
        ids = [item.get("id") for item in (listing.json or {}).get("items") or []]
        self.expect(project_id in ids, "project is missing from the projects list")

        data_dir = self.report.environment.get("data_dir")
        db_path = None
        if data_dir:
            candidate = Path(data_dir) / "voice_atelier.sqlite3"
            self.expect(candidate.is_file(), f"sqlite database not found at {candidate}")
            self.expect(candidate.stat().st_size > 0, "sqlite database is empty")
            db_path = {"path": str(candidate), "bytes": candidate.stat().st_size}

        self.report.add(
            "persistence:project",
            "PASS",
            started,
            detail={
                "project_id": project_id,
                "segments": len(segments),
                "takes": takes_now,
                "listed": True,
                "database": db_path,
            },
        )

    def step_gpu(self) -> None:
        started = time.monotonic()
        target = self.artifacts_dir / "nvidia-smi.txt"
        try:
            with target.open("w", encoding="utf-8") as handle:
                handle.write(f"$ nvidia-smi (captured {utcnow()})\n\n")
                handle.flush()
                subprocess.run(  # noqa: S603 - fixed argv
                    [
                        "nvidia-smi",
                        "--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu",
                        "--format=csv",
                    ],
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    timeout=30,
                )
            self.report.artifact(target, "GPU snapshot (informational only)")
            text = target.read_text(encoding="utf-8", errors="replace")
            self.report.gpu = {
                "available": True,
                "file": str(target),
                "first_lines": text.splitlines()[:4],
                "note": "informational; peak VRAM during synthesis is NOT measured by this script",
            }
            self.report.add("gpu:nvidia-smi", "PASS", started, detail={"file": str(target)})
        except FileNotFoundError:
            self.report.gpu = {"available": False}
            self.report.add(
                "gpu:nvidia-smi",
                "SKIP",
                started,
                detail={"reason": "nvidia-smi not found; engine device info comes from /api/status"},
            )
        except subprocess.TimeoutExpired:
            self.report.gpu = {"available": False}
            self.report.add("gpu:nvidia-smi", "SKIP", started, detail={"reason": "nvidia-smi timed out"})

    def run_browser(self) -> None:
        """Run the browser smoke in-process against the *same* live server.

        The browser verdict is deliberately independent of the audio verdict: a missing
        Playwright/Edge or a UI defect must never be reported as an engine failure.
        """
        started = time.monotonic()
        if self.server is None and not self.external_server:
            self.report.add("browser:smoke", "BLOCKED", started, error="own server is not running")
            return
        if not (self.verification_dir / "browser_smoke.py").is_file():
            self.report.add(
                "browser:smoke", "SKIP", started, detail={"reason": "verification/browser_smoke.py missing"}
            )
            return
        try:
            if str(self.verification_dir) not in sys.path:
                sys.path.insert(0, str(self.verification_dir))
            import browser_smoke  # noqa: PLC0415 - local sibling module, imported lazily
        except Exception as error:  # noqa: BLE001
            self.report.add(
                "browser:smoke", "SKIP", started, detail={"reason": f"cannot import browser_smoke: {error}"}
            )
            return

        argv = [
            "--project-root",
            str(self.root),
            "--base-url",
            self.base_url,
            "--port",
            str(self.args.port),
            "--python",
            self.args.python,
            "--artifacts-dir",
            str(self.artifacts_dir / "browser"),
        ]
        if self.args.home:
            argv += ["--home", str(self.args.home)]
        if self.args.browser_headed:
            argv += ["--headed"]
        if self.args.keep_browser_project:
            argv += ["--keep-project"]

        try:
            rc = browser_smoke.main(argv)
        except SystemExit as exc:  # argparse or defensive exit
            rc = int(exc.code or 0)
        except Exception as error:  # noqa: BLE001
            self.report.add("browser:smoke", "FAIL", started, error=f"{type(error).__name__}: {error}")
            self.browser_result = {"verdict": "FAIL", "reason": str(error)}
            return

        verdict = {0: "PASS", 1: "FAIL", 3: "NOT RUN"}.get(rc, f"EXIT {rc}")
        # "NOT RUN" is surfaced as BLOCKED so it is visible in the counts, but it is
        # never counted as an audio failure and never changes the audio exit code.
        status = {"PASS": "PASS", "FAIL": "FAIL"}.get(verdict, "BLOCKED")
        self.browser_result = {
            "exit_code": rc,
            "verdict": verdict,
            "independent_of_audio": True,
            "report": str(self.artifacts_dir / "browser" / "browser-report.json"),
            "screenshots_dir": str(self.artifacts_dir / "browser"),
        }
        self.report.add("browser:smoke", status, started, detail=dict(self.browser_result))
        if verdict != "PASS":
            print(
                f"[info] browser smoke: {verdict} (audio verdict is unaffected; see the browser report)",
                flush=True,
            )

    # -- orchestration -----------------------------------------------------------------
    def run(self) -> int:
        self.preflight()
        if self.args.pytest:
            self.run_pytest()

        self.start_server()
        self.wait_health()
        self.fetch_token()
        self.client = Client(
            self.base_url, token=self.token, timeout=self.args.http_timeout, recorder=self._record_call
        )
        self.report.environment["token_source"] = "cli" if self.args.token else "index meta"

        started = time.monotonic()
        try:
            self.step_status_and_models()
        except (StepError, DriverError, OSError, ValueError) as error:
            self.report.add("server:status", "FAIL", started, error=str(error))
            self.exit_code = max(self.exit_code, 3)
            return self.exit_code

        started = time.monotonic()
        try:
            self.step_negative()
        except (StepError, DriverError, OSError, ValueError) as error:
            self.report.add("api:negative-checks", "FAIL", started, error=str(error))
            self.exit_code = max(self.exit_code, 1)

        self.step_gpu()

        started = time.monotonic()
        try:
            self.step_download()
        except (StepError, DriverError, OSError, ValueError) as error:
            self.report.add("models:download", "FAIL", started, error=str(error))
            self.exit_code = max(self.exit_code, 1)
            self.models_ready = False

        if not self.require_models():
            for name, reason in (
                ("audio:design", "models are not installed locally; nothing was downloaded by default"),
                ("audio:clone", "depends on the design voice, which needs installed weights"),
                ("export:zip+wav", "depends on generated takes"),
                ("persistence:project", "depends on the generated project"),
            ):
                self.report.add(name, "BLOCKED", time.monotonic(), error=reason)
        else:
            design_ok = False
            started = time.monotonic()
            try:
                design_ok = self.step_design()
            except (StepError, DriverError, OSError, ValueError) as error:
                self.report.add("audio:design", "FAIL", started, error=str(error))
                self.exit_code = max(self.exit_code, 1)
            if design_ok:
                clone_ok = False
                started = time.monotonic()
                try:
                    clone_ok = self.step_clone_project()
                except (StepError, DriverError, OSError, ValueError) as error:
                    self.report.add("audio:clone", "FAIL", started, error=str(error))
                    self.exit_code = max(self.exit_code, 1)
                if clone_ok:
                    started = time.monotonic()
                    try:
                        self.step_export()
                    except (StepError, DriverError, OSError, ValueError) as error:
                        self.report.add("export:zip+wav", "FAIL", started, error=str(error))
                        self.exit_code = max(self.exit_code, 1)
                    started = time.monotonic()
                    try:
                        self.step_persistence()
                    except (StepError, DriverError, OSError, ValueError) as error:
                        self.report.add("persistence:project", "FAIL", started, error=str(error))
                        self.exit_code = max(self.exit_code, 1)
                else:
                    self.report.add(
                        "export:zip+wav",
                        "BLOCKED",
                        time.monotonic(),
                        error="clone generation failed; nothing to export",
                    )
                    self.report.add(
                        "persistence:project",
                        "BLOCKED",
                        time.monotonic(),
                        error="clone generation failed; no project state to re-read",
                    )

        if self.args.browser:
            self.run_browser()

        # A browser failure is reported as FAIL in the JSON, but it must not turn a
        # successful audio run into a failed one (they are different contracts).
        audio_failures = [
            step
            for step in self.report.steps
            if step.status == "FAIL" and not step.name.startswith("browser:")
        ]
        if audio_failures:
            self.exit_code = max(self.exit_code, 1)
        return self.exit_code

    def write_report(self) -> Path:
        target = self.artifacts_dir / "smoke-report.json"
        payload = self.report.to_dict()
        payload["environment"].update(self.report.environment)
        payload["endpoints"] = self.calls
        payload["browser"] = self.browser_result
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return target


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def default_python(root: Path) -> str:
    for relative in ("Scripts/python.exe", "bin/python"):
        candidate = root / ".venv" / relative
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Real integration smoke for Voice Atelier (starts the real server)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--python", default=None, help="interpreter used to run the server (default: project .venv)")
    parser.add_argument("--home", default=None, help="override VOICE_ATELIER_HOME / --home for the server")
    parser.add_argument("--artifacts-dir", default=None)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--boot-timeout", type=float, default=180.0)
    parser.add_argument("--http-timeout", type=float, default=60.0)
    parser.add_argument("--poll-seconds", type=float, default=3.0, help="job poll interval (not a busy loop)")
    parser.add_argument(
        "--job-timeout",
        type=float,
        default=3 * 60 * 60,
        help="per-job ceiling; real 1.7B synthesis on 8 GB can take a long time",
    )
    parser.add_argument("--export-timeout", type=float, default=600.0)
    parser.add_argument("--pytest-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--download-approved",
        action="store_true",
        help="explicitly allow POST /api/models/{id}/download with accept_license (user approved)",
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="attach to an already running studio on --port instead of starting one (never terminates it)",
    )
    parser.add_argument("--token", default=None, help="token for --no-server (default: scrape index)")
    parser.add_argument("--pytest", action="store_true", help="run pytest tests/ first, in a subprocess")
    parser.add_argument(
        "--browser",
        action="store_true",
        help="also run verification/browser_smoke.py in-process against the live server",
    )
    parser.add_argument("--browser-headed", action="store_true", help="show the browser window")
    parser.add_argument(
        "--keep-browser-project", action="store_true", help="keep the UI-generated test project"
    )
    parser.add_argument(
        "--allow-download-after-failed-tests",
        action="store_true",
        help="dangerous: allow weight downloads even if the pytest run failed",
    )
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()
    args.project_root = str(root)
    args.python = args.python or default_python(root)
    args.artifacts_dir = args.artifacts_dir or str(root / "verification" / "artifacts")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    driver = Driver(args)
    try:
        exit_code = driver.run()
    except DriverError as error:
        driver.report.fatal = str(error)
        print(f"[FAIL] fatal: {error}", file=sys.stderr, flush=True)
        driver.exit_code = max(driver.exit_code, 3)
        exit_code = driver.exit_code
    except Exception:  # noqa: BLE001 - report unexpected driver breakage honestly
        driver.report.fatal = traceback.format_exc(limit=6)
        print("[FAIL] unexpected driver error:\n" + driver.report.fatal, file=sys.stderr, flush=True)
        driver.exit_code = max(driver.exit_code, 3)
        exit_code = driver.exit_code
    finally:
        driver.shutdown()

    report_path = driver.write_report()
    counts = driver.report.counts()
    print(
        "\nsummary: "
        + ", ".join(f"{key}={value}" for key, value in counts.items())
        + f"\nreport:  {report_path}",
        flush=True,
    )
    if driver.report.fatal:
        print("note: driver aborted before all steps ran", flush=True)
    if driver.browser_result is not None:
        print(
            "browser: "
            + str(driver.browser_result.get("verdict"))
            + " (separate from the audio verdict; see "
            + str(driver.browser_result.get("report"))
            + ")",
            flush=True,
        )
    print(
        "honesty: objective audio checks only (decode, duration, non-silence). "
        "No quality or similarity claim, no audition.",
        flush=True,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
