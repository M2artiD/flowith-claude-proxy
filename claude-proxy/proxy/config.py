"""Runtime configuration for the Flowith -> Claude proxy."""

from __future__ import annotations

import json
import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_PROJECT_ROOT / ".env", override=False)
except ImportError:
    pass

# Upstream (Flowith LLM endpoint)
FLOWITH_BASE_URL = os.environ.get(
    "FLOWITH_BASE_URL",
    "https://edge.flowith.io/external/use/llm",
)

DEFAULT_MODEL = os.environ.get("FLOWITH_DEFAULT_MODEL", "claude-4.6-sonnet")

API_TIMEOUT = int(os.environ.get("FLOWITH_TIMEOUT", "120"))

# Upstream stability tuning
FLOWITH_MAX_CONCURRENCY = int(os.environ.get("FLOWITH_MAX_CONCURRENCY", "3"))
FLOWITH_POOL_MAXSIZE = int(os.environ.get("FLOWITH_POOL_MAXSIZE", str(max(4, FLOWITH_MAX_CONCURRENCY))))
FLOWITH_RETRY_TOTAL = int(os.environ.get("FLOWITH_RETRY_TOTAL", "3"))
FLOWITH_RETRY_BACKOFF = float(os.environ.get("FLOWITH_RETRY_BACKOFF", "0.75"))
FLOWITH_RETRY_JITTER = float(os.environ.get("FLOWITH_RETRY_JITTER", "0.5"))

FLOWITH_TOOL_MODE = os.environ.get("FLOWITH_TOOL_MODE", "xml").strip().lower()
if FLOWITH_TOOL_MODE not in {"xml", "native"}:
    FLOWITH_TOOL_MODE = "xml"

FLOWITH_SSL_VERIFY = os.environ.get("FLOWITH_SSL_VERIFY", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

# HTTP/SOCKS proxy
_https_proxy = os.environ.get("FLOWITH_UPSTREAM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
_http_proxy = os.environ.get("FLOWITH_UPSTREAM_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
UPSTREAM_PROXIES: dict[str, str] | None = None
if _https_proxy:
    UPSTREAM_PROXIES = {"https": _https_proxy, "http": _http_proxy or _https_proxy}

# Debug dump
DEBUG_DUMP = os.environ.get("FLOWITH_DEBUG_DUMP", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
DEBUG_DUMP_DIR = os.environ.get("FLOWITH_DEBUG_DUMP_DIR", str(_PROJECT_ROOT / "debug_dumps"))

# Server bind
DEFAULT_HOST = os.environ.get("FLOWITH_API_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("FLOWITH_API_PORT", "8787"))

# Custom model aliases
CUSTOM_MODEL_ALIASES: dict[str, str] = {}
_raw_aliases = os.environ.get("FLOWITH_MODEL_ALIASES", "").strip()
if _raw_aliases:
    try:
        CUSTOM_MODEL_ALIASES = {
            str(k): str(v) for k, v in json.loads(_raw_aliases).items()
        }
    except (json.JSONDecodeError, AttributeError):
        pass


def load_api_key() -> str | None:
    key = os.environ.get("FLOWITH_API_KEY")
    if key:
        return key.strip()
    for candidate in (_PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("FLOWITH_API_KEY="):
                        value = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if value:
                            return value
        except Exception:
            continue
    return None
