"""Entry point: python -m studio --port 8765"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import LOOPBACK_HOSTS, Settings


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="studio", description="Voice Atelier local server")
    parser.add_argument("--host", default=None, help="loopback host (127.0.0.1 or localhost)")
    parser.add_argument("--port", type=int, default=None, help="port (default 8765)")
    parser.add_argument("--home", default=None, help="data directory (default <repo>/data)")
    parser.add_argument("--log-level", default="info")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = Settings.from_env(home=Path(args.home) if args.home else None)
    if args.host:
        settings.host = args.host
    if args.port:
        settings.port = args.port
    if settings.host not in LOOPBACK_HOSTS:
        print(
            f"refusing non-loopback host {settings.host!r}; this server only binds to localhost",
            file=sys.stderr,
        )
        return 2

    from .app import create_app

    app = create_app(settings)
    print(f"Voice Atelier data: {settings.home}")
    print(f"Voice Atelier URL:  http://{settings.host}:{settings.port}")

    import uvicorn

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
