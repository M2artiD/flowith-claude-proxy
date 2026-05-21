"""Entry point: ``python -m flowith_claude_proxy``."""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    FLOWITH_BASE_URL,
    load_api_key,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="flowith-claude-proxy",
        description="Run a Claude-compatible API server backed by Flowith.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload (dev only)"
    )
    parser.add_argument(
        "--version", action="version", version=f"flowith-claude-proxy {__version__}"
    )
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print(
            "uvicorn is required. Install with:  pip install fastapi uvicorn",
            file=sys.stderr,
        )
        sys.exit(1)

    if not load_api_key():
        print(
            "[warn] FLOWITH_API_KEY is not set on the server. "
            "Clients must send their own x-api-key / Authorization header.",
            file=sys.stderr,
        )

    base = f"http://{args.host}:{args.port}"
    print(f"Flowith Claude-Compatible Proxy v{__version__}")
    print(f"  Listening:    {base}")
    print(f"  Upstream:     {FLOWITH_BASE_URL}")
    print(f"  Endpoint:     POST {base}/v1/messages")
    print()
    print("Use with cc-switch / Claude Code by setting:")
    print(f"  ANTHROPIC_BASE_URL = {base}")
    print(f"  ANTHROPIC_API_KEY  = <your Flowith key>")
    print()

    uvicorn.run(
        "flowith_claude_proxy.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
