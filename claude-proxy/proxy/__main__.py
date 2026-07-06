"""Command-line entry point."""

from __future__ import annotations

import io
import os
import sys

import uvicorn

from . import __version__
from .config import DEFAULT_HOST, DEFAULT_PORT, FLOWITH_BASE_URL, FLOWITH_LOCAL_ONLY


def _configure_stdio_for_windows() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        buffer = getattr(stream, "buffer", None)
        if buffer is None:
            continue
        setattr(
            sys,
            stream_name,
            io.TextIOWrapper(buffer, encoding="utf-8", errors="replace"),
        )
    if os.name == "nt":
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    _configure_stdio_for_windows()
    print(f"""
=====================================
  Flowith Claude Proxy v{__version__}
=====================================

  Address:    http://{DEFAULT_HOST}:{DEFAULT_PORT}
  Upstream:   {FLOWITH_BASE_URL}
  Local only: {str(FLOWITH_LOCAL_ONLY).lower()}

  API Docs:   http://{DEFAULT_HOST}:{DEFAULT_PORT}/docs
  Health:     http://{DEFAULT_HOST}:{DEFAULT_PORT}/health
""")

    uvicorn.run(
        "proxy.server:app",
        host=DEFAULT_HOST,
        port=DEFAULT_PORT,
    )
