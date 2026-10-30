"""Small filesystem helpers; every stored path is generated, never client supplied."""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path


def new_id() -> str:
    return str(uuid.uuid4())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_replace(src: Path, dst: Path) -> None:
    """Move a completed temp file into place (same volume)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def safe_unlink(path: Path | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
