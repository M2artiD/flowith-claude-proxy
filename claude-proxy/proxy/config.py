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

def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


# Upstream (Flowith LLM endpoint)
FLOWITH_BASE_URL = os.environ.get(
    "FLOWITH_BASE_URL",
    "https://edge.flowith.io/external/use/llm",
)

DEFAULT_MODEL = os.environ.get("FLOWITH_DEFAULT_MODEL", "claude-4.6-sonnet")

API_TIMEOUT = _env_int("FLOWITH_TIMEOUT", 300)

# Upstream stability tuning
FLOWITH_CONNECT_TIMEOUT = _env_float("FLOWITH_CONNECT_TIMEOUT", 30)
FLOWITH_MAX_CONCURRENCY = _env_int("FLOWITH_MAX_CONCURRENCY", 8)
FLOWITH_SEMAPHORE_TIMEOUT = _env_float("FLOWITH_SEMAPHORE_TIMEOUT", 30)
FLOWITH_POOL_MAXSIZE = _env_int("FLOWITH_POOL_MAXSIZE", max(16, FLOWITH_MAX_CONCURRENCY * 2))
FLOWITH_RETRY_TOTAL = _env_int("FLOWITH_RETRY_TOTAL", 6)
FLOWITH_RETRY_BACKOFF = _env_float("FLOWITH_RETRY_BACKOFF", 0.5)
FLOWITH_RETRY_JITTER = _env_float("FLOWITH_RETRY_JITTER", 0.25)
FLOWITH_RETRY_MAX_DELAY = _env_float("FLOWITH_RETRY_MAX_DELAY", 8)
FLOWITH_SSL_RETRY_EXTRA = _env_int("FLOWITH_SSL_RETRY_EXTRA", 10)
FLOWITH_DISABLE_KEEPALIVE = os.environ.get("FLOWITH_DISABLE_KEEPALIVE", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

FLOWITH_TOOL_MODE = os.environ.get("FLOWITH_TOOL_MODE", "xml").strip().lower()
if FLOWITH_TOOL_MODE not in {"xml", "native"}:
    FLOWITH_TOOL_MODE = "xml"

FLOWITH_SSL_VERIFY = os.environ.get("FLOWITH_SSL_VERIFY", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

FLOWITH_TRACE_HERMES = os.environ.get("FLOWITH_TRACE_HERMES", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

FLOWITH_REQUEST_LOG = os.environ.get("FLOWITH_REQUEST_LOG", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

FLOWITH_REQUIRE_SERVER_KEY = os.environ.get("FLOWITH_REQUIRE_SERVER_KEY", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

FLOWITH_LOCAL_ONLY = os.environ.get("FLOWITH_LOCAL_ONLY", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

# Reject request bodies above this many bytes before FastAPI reads the JSON
# payload into memory. A value <= 0 disables the guard.
FLOWITH_MAX_REQUEST_BYTES = _env_int("FLOWITH_MAX_REQUEST_BYTES", 20 * 1024 * 1024)

# HTTP/SOCKS proxy
_https_proxy = os.environ.get("FLOWITH_UPSTREAM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
_http_proxy = os.environ.get("FLOWITH_UPSTREAM_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
UPSTREAM_PROXIES: dict[str, str] | None = None
if _https_proxy:
    UPSTREAM_PROXIES = {"https": _https_proxy, "http": _http_proxy or _https_proxy}

def _env_path(name: str, default: Path) -> str:
    value = os.environ.get(name, "").strip()
    return value or str(default)


# Debug dump
DEBUG_DUMP = os.environ.get("FLOWITH_DEBUG_DUMP", "false").strip().lower() in {
    "1", "true", "yes", "on",
}
DEBUG_DUMP_DIR = _env_path("FLOWITH_DEBUG_DUMP_DIR", _PROJECT_ROOT / "debug_dumps")
DEBUG_DUMP_MAX_BYTES = _env_int("FLOWITH_DEBUG_DUMP_MAX_BYTES", 1024 * 1024)
DEBUG_DUMP_MAX_FILES = _env_int("FLOWITH_DEBUG_DUMP_MAX_FILES", 100)

# Server bind
DEFAULT_HOST = os.environ.get("FLOWITH_API_HOST", "127.0.0.1")
DEFAULT_PORT = _env_int("FLOWITH_API_PORT", 8787)

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


_PLACEHOLDER_API_KEYS = {
    "your flowith api key",
    "your-flowith-api-key",
    "your_api_key_here",
}


def _clean_api_key(value: str) -> str:
    return value.strip().strip('"').strip("'")


def _env_example_api_key() -> str | None:
    candidate = _PROJECT_ROOT / ".env.example"
    if not candidate.exists():
        return None
    try:
        with candidate.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("FLOWITH_API_KEY="):
                    return _clean_api_key(line.split("=", 1)[1])
    except Exception:
        return None
    return None


def _is_placeholder_api_key(value: str) -> bool:
    cleaned = _clean_api_key(value).lower()
    example_key = _env_example_api_key()
    example_keys = {example_key.lower()} if example_key else set()
    return cleaned in _PLACEHOLDER_API_KEYS | example_keys


def _valid_api_key_or_none(value: str) -> str | None:
    cleaned = _clean_api_key(value)
    if not cleaned or _is_placeholder_api_key(cleaned):
        return None
    return cleaned


def load_api_key() -> str | None:
    key = os.environ.get("FLOWITH_API_KEY")
    if key:
        value = _valid_api_key_or_none(key)
        if value:
            return value

    for candidate in (
        _PROJECT_ROOT / ".flowith_api_key",
        Path.cwd() / ".flowith_api_key",
    ):
        if not candidate.exists():
            continue
        try:
            value = candidate.read_text(encoding="utf-8").strip().strip('"').strip("'")
        except Exception:
            continue
        value = _valid_api_key_or_none(value)
        if value:
            return value

    for candidate in (_PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if not candidate.exists():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("FLOWITH_API_KEY="):
                        value = line.split("=", 1)[1].strip().strip('"').strip("'")
                        value = _valid_api_key_or_none(value)
                        if value:
                            return value
        except Exception:
            continue
    return None
