"""Explicit, pinned, safetensors-only Hugging Face downloader.

Never invoked automatically: only a user-confirmed job calls this. No pickle,
.pt, .bin or .ckpt files are fetched; every file is allowlist-matched.

Integrity is verified against trusted Hugging Face metadata fetched for the
pinned revision: LFS files by content SHA-256, regular Git blobs by the Git blob
SHA-1 (``oid``) of the content. A file with no verifiable expected digest fails
closed; local hashes are never treated as proof on their own.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import Settings
from ..errors import EngineCancelled, EngineError
from ..storage import sha256_file
from .catalog import (
    ALLOWED_PATTERNS,
    MODELS,
    REQUIRED_FILES,
    ModelSpec,
    installed_state,
    model_dir,
)

ProgressFn = Callable[[float, str], None]
CancelFn = Callable[[], bool]


@dataclass(frozen=True)
class ExpectedFile:
    name: str
    size: int | None
    lfs_sha256: str | None
    blob_id: str | None


def _allowed(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pattern) for pattern in ALLOWED_PATTERNS)


def _validate_safetensors(path: Path) -> None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(8)
            if len(raw) != 8:
                raise ValueError("truncated header")
            header_len = int.from_bytes(raw, "little")
            if header_len <= 0 or header_len > 200_000_000:
                raise ValueError("implausible header length")
            header = json.loads(handle.read(header_len))
        if not isinstance(header, dict) or not header:
            raise ValueError("empty header")
        tensors = [key for key in header if key != "__metadata__"]
        if not tensors:
            raise ValueError("no tensors in header")
    except Exception as exc:
        raise EngineError(
            "invalid_weights", f"{path.name} is not a valid safetensors file: {exc}"
        ) from exc


def _git_blob_sha1(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(b"blob %d\0" % size)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata_from_model_info(api, spec: ModelSpec) -> dict[str, ExpectedFile] | None:
    try:
        info = api.model_info(spec.repo, revision=spec.revision, files_metadata=True)
    except TypeError:
        return None
    except Exception as exc:
        raise EngineError(
            "download_failed", f"Could not read metadata for {spec.repo}@{spec.revision}: {exc}"
        ) from exc
    siblings = getattr(info, "siblings", None)
    if not siblings:
        return None
    expected: dict[str, ExpectedFile] = {}
    for sibling in siblings:
        name = getattr(sibling, "rfilename", None)
        if not name:
            continue
        lfs = getattr(sibling, "lfs", None)
        lfs_sha = getattr(lfs, "sha256", None) if lfs is not None else None
        size = getattr(sibling, "size", None)
        blob_id = getattr(sibling, "blob_id", None)
        if size is None and lfs is not None:
            size = getattr(lfs, "size", None)
        if lfs_sha is None and size is None and blob_id is None:
            # Metadata requested but not materialised: treat as unusable.
            continue
        expected[name] = ExpectedFile(
            name=name, size=size, lfs_sha256=lfs_sha, blob_id=blob_id
        )
    return expected or None


def _metadata_from_tree(api, spec: ModelSpec) -> dict[str, ExpectedFile] | None:
    try:
        entries = api.list_repo_tree(spec.repo, revision=spec.revision, recursive=True)
    except Exception as exc:
        raise EngineError(
            "download_failed", f"Could not read file tree for {spec.repo}@{spec.revision}: {exc}"
        ) from exc
    expected: dict[str, ExpectedFile] = {}
    for entry in entries:
        name = getattr(entry, "path", None)
        if not name:
            continue
        lfs = getattr(entry, "lfs", None)
        expected[name] = ExpectedFile(
            name=name,
            size=getattr(entry, "size", None),
            lfs_sha256=getattr(lfs, "sha256", None) if lfs is not None else None,
            blob_id=getattr(entry, "oid", None),
        )
    return expected or None


def verify_file(path: Path, expected: ExpectedFile) -> str:
    """Return the verification kind; raise EngineError if it cannot be proven."""
    if expected.size is not None and path.stat().st_size != expected.size:
        raise EngineError(
            "integrity_mismatch",
            f"{expected.name}: size {path.stat().st_size} != expected {expected.size}",
        )
    if expected.lfs_sha256:
        actual = sha256_file(path)
        if actual.lower() != expected.lfs_sha256.lower():
            raise EngineError(
                "integrity_mismatch",
                f"{expected.name}: SHA-256 does not match the pinned revision metadata",
            )
        return "lfs-sha256"
    if expected.blob_id:
        actual = _git_blob_sha1(path)
        if actual.lower() != expected.blob_id.lower():
            raise EngineError(
                "integrity_mismatch",
                f"{expected.name}: Git blob SHA-1 does not match the pinned revision metadata",
            )
        return "git-blob-sha1"
    raise EngineError(
        "unverified_download",
        f"{expected.name}: Hugging Face returned no expected digest; refusing unverified file",
    )


def _hf_api():
    from huggingface_hub import HfApi  # noqa: PLC0415

    return HfApi()


def _hf_download(**kwargs):
    from huggingface_hub import hf_hub_download  # noqa: PLC0415

    return hf_hub_download(**kwargs)


_TRANSIENT_MARKERS = (
    "incompleteread",
    "incomplete read",
    "connection",
    "reset by peer",
    "server disconnected",
    "remotedisconnected",
    "chunkedencodingerror",
    "protocolerror",
    "timed out",
    "timeout",
    "temporarily",
    "temporary failure",
    "502",
    "503",
    "504",
    "429",
    "eof occurred",
)
_PERMANENT_MARKERS = (
    "401",
    "403",
    "404",
    "unauthorized",
    "forbidden",
    "not found",
    "gated",
    "authentication",
    "requires login",
    "invalid username",
)


def _is_transient_download_error(exc: Exception) -> bool:
    text = str(exc).lower()
    if any(marker in text for marker in _PERMANENT_MARKERS):
        return False
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def _download_file_with_retry(
    filename: str,
    spec: ModelSpec,
    target: Path,
    cancelled: CancelFn,
    delays: tuple[float, ...] = (1.5, 3.0),
) -> None:
    """Bounded retry for transient network failures with cancellable backoff.

    hf_hub_download resumes/keeps partial files itself; permanent 404/401/403
    errors fail immediately. The verified manifest is never written here.
    """
    last: Exception | None = None
    for attempt in range(3):
        if cancelled():
            raise EngineCancelled()
        try:
            _hf_download(
                repo_id=spec.repo,
                filename=filename,
                revision=spec.revision,
                local_dir=str(target),
            )
            return
        except Exception as exc:  # noqa: BLE001 - normalized below
            last = exc
            if not _is_transient_download_error(exc) or attempt == 2:
                raise EngineError(
                    "download_failed", f"Download failed for {filename}: {exc}"
                ) from exc
            delay = delays[min(attempt, len(delays) - 1)]
            deadline = time.monotonic() + max(0.0, delay)
            while time.monotonic() < deadline:
                if cancelled():
                    raise EngineCancelled()
                time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))
    raise EngineError("download_failed", f"Download failed for {filename}: {last}")


def try_reuse_local_file(
    settings: Settings,
    spec: ModelSpec,
    target: Path,
    filename: str,
    expected: ExpectedFile,
) -> str | None:
    """Copy an identical file from another *verified* catalog model directory.

    The source model must be fully installed with a verified manifest; the
    manifest entry's expected digest must equal the target's expected digest;
    the source file's actual digest is re-checked before copying, and the copied
    file is verified again against the target expectation. No network, no
    hardlinks, and only the three catalog model directories are searched.
    """
    destination = target / filename
    for other in MODELS:
        if other.id == spec.id:
            continue
        other_dir = model_dir(settings, other.id)
        if not other_dir.is_dir():
            continue
        installed, _status, _problems = installed_state(settings, other)
        if not installed:
            continue
        try:
            manifest = json.loads((other_dir / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        entry = None
        for candidate in manifest.get("files") or []:
            if isinstance(candidate, dict) and candidate.get("name") == filename:
                entry = candidate
                break
        if entry is None:
            continue
        if expected.lfs_sha256:
            if entry.get("expected_sha256") != expected.lfs_sha256:
                continue
        elif expected.blob_id:
            if entry.get("expected_blob_id") != expected.blob_id:
                continue
        else:
            continue
        source = other_dir / filename
        if not source.is_file():
            continue
        try:
            verify_file(source, expected)
        except EngineError:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, destination)
            verify_file(destination, expected)
        except (EngineError, OSError):
            try:
                destination.unlink()
            except OSError:
                pass
            continue
        return other.id
    return None


def download_model(
    settings: Settings,
    spec: ModelSpec,
    progress: ProgressFn,
    cancelled: CancelFn,
) -> dict:
    if settings.offline:
        raise EngineError("offline", "Downloads are disabled (VOICE_ATELIER_OFFLINE=1)")
    try:
        api = _hf_api()
    except Exception as exc:  # pragma: no cover - environment dependent
        raise EngineError("engine_unavailable", f"huggingface_hub is missing: {exc}") from exc

    target = model_dir(settings, spec.id)
    target.mkdir(parents=True, exist_ok=True)

    expected_map = _metadata_from_model_info(api, spec)
    if expected_map is None:
        expected_map = _metadata_from_tree(api, spec)
    if not expected_map:
        raise EngineError(
            "download_failed",
            "Hugging Face metadata is unavailable; refusing an unverifiable download",
        )
    files = sorted(name for name in expected_map if _allowed(name))
    if not files:
        raise EngineError("download_failed", "No allowlisted files found in the pinned revision")
    missing_required = [name for name in REQUIRED_FILES if name not in expected_map]
    if missing_required:
        raise EngineError(
            "download_incomplete",
            "Repository does not contain the expected files",
            {"missing": missing_required},
        )

    # Invalidate any previous manifest before touching weights: a canceled or
    # restarted download must never leave an old manifest claiming a good install.
    for stale in target.glob("manifest.json*"):
        try:
            stale.unlink()
        except OSError:
            pass

    total = len(files)
    reused_map: dict[str, str] = {}
    for index, filename in enumerate(files):
        if cancelled():
            raise EngineCancelled()
        expected = expected_map[filename]
        local_path = target / filename
        if local_path.is_file():
            try:
                verify_file(local_path, expected)
                reused_map[filename] = "already-present"
                progress(index / max(total, 1), f"reusing local {index + 1}/{total}: {filename}")
                continue
            except EngineError:
                pass
        reused_from = try_reuse_local_file(settings, spec, target, filename, expected)
        if reused_from:
            reused_map[filename] = reused_from
            progress(
                index / max(total, 1),
                f"reusing {index + 1}/{total}: {filename} from {reused_from}",
            )
            continue
        progress(index / max(total, 1), f"downloading {index + 1}/{total}: {filename}")
        _download_file_with_retry(filename, spec, target, cancelled)

    progress(0.95, "verifying against pinned revision metadata")
    entries = []
    for filename in files:
        file_path = target / filename
        if not file_path.is_file():
            raise EngineError("download_incomplete", f"Missing downloaded file: {filename}")
        expected = expected_map[filename]
        verified_by = verify_file(file_path, expected)
        if filename.endswith(".safetensors"):
            _validate_safetensors(file_path)
        entries.append(
            {
                "name": filename,
                "size": file_path.stat().st_size,
                "sha256": sha256_file(file_path),
                "expected_sha256": expected.lfs_sha256,
                "expected_blob_id": expected.blob_id,
                "verified_by": verified_by,
                "reused_from": reused_map.get(filename),
            }
        )
    still_missing = [name for name in REQUIRED_FILES if not (target / name).is_file()]
    if still_missing:
        raise EngineError(
            "download_incomplete",
            "Downloaded model is missing required files",
            {"missing": still_missing},
        )
    manifest = {
        "model_id": spec.id,
        "repo": spec.repo,
        "revision": spec.revision,
        "license": spec.license,
        "verified": True,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "files": entries,
    }
    # Publish atomically: a crash never leaves a half-written manifest.
    manifest_path = target / "manifest.json"
    temp_manifest = target / f"manifest.json.tmp-{uuid.uuid4().hex}"
    temp_manifest.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(temp_manifest, manifest_path)
    progress(1.0, "installed")
    return manifest


def read_manifest(settings: Settings, model_id: str) -> dict | None:
    path = model_dir(settings, model_id) / "manifest.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
