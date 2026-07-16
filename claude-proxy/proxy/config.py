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

DEFAULT_MODEL = os.environ.get("FLOWITH_DEFAULT_MODEL", "claude-5-sonnet")

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
# Flowith can return HTTP 200 with no text, reasoning, or tool call. Short retries
# recover isolated provider glitches, but large Fable contexts can deterministically
# return empty forever. Keep this budget short so an unrecoverable request becomes
# an explicit response instead of making the client appear hung for minutes.
FLOWITH_EMPTY_RETRY_WINDOW = _env_float("FLOWITH_EMPTY_RETRY_WINDOW", 8.0)
FLOWITH_EMPTY_RETRY_DELAY = _env_float("FLOWITH_EMPTY_RETRY_DELAY", 0.35)
FLOWITH_EMPTY_RETRY_DELAY_MAX = _env_float("FLOWITH_EMPTY_RETRY_DELAY_MAX", 1.0)
# Hard upper bound on extra empty-response attempts. Set to 0 only when a finite
# FLOWITH_EMPTY_RETRY_WINDOW is configured and an uncapped attempt count is wanted.
FLOWITH_EMPTY_RETRY_TOTAL = _env_int("FLOWITH_EMPTY_RETRY_TOTAL", 8)
# If an already-retried request is still empty, retain only the system prompt and
# newest conversation messages below this character budget for one final attempt.
# Set to 0 to disable this lossy recovery path.
FLOWITH_EMPTY_CONTEXT_FALLBACK_CHARS = _env_int("FLOWITH_EMPTY_CONTEXT_FALLBACK_CHARS", 90000)
# Fable starts returning empty streams well before its advertised context size.
# Compact only Fable requests above this budget before sending them upstream.
# Set to 0 to disable preemptive compaction.
FLOWITH_FABLE_CONTEXT_COMPACT_CHARS = _env_int("FLOWITH_FABLE_CONTEXT_COMPACT_CHARS", 90000)
# Once Fable still returns empty after compaction/retries, switch models instead
# of reporting a successful-looking empty turn. Defaults to the normal proxy
# model; set blank to disable or choose another non-Fable Flowith model.
FLOWITH_FABLE_FALLBACK_MODEL = os.environ.get(
    "FLOWITH_FABLE_FALLBACK_MODEL",
    DEFAULT_MODEL,
).strip()
# A dead upstream stream delivers zero content for its whole life (measured:
# 240-277s of silence) before finally hanging or sending an empty terminal
# frame. A healthy stream, by contrast, delivers its first byte within ~83s
# (measured p90/max across OK samples) and then keeps flowing. So a per-stream
# idle watchdog cleanly separates the two: if no content/reasoning/tool delta
# arrives for this many seconds, abort the stream early and retry on the
# empty-stream path instead of waiting out the full dead-stream lifetime. The
# default clears the observed OK first-byte max (~83s) with generous margin
# while sitting well below the dead-stream floor (~240s). Set to 0 to disable.
FLOWITH_STREAM_IDLE_TIMEOUT = _env_float("FLOWITH_STREAM_IDLE_TIMEOUT", 150.0)
# SSE comments keep the TCP connection active but are not events, so clients
# such as Codex may still expire their event-level idle timer. Responses routes
# emit a protocol-level in-progress event at this cadence while awaiting the
# next upstream delta. Keep this comfortably below common client idle limits.
FLOWITH_SSE_HEARTBEAT_INTERVAL = _env_float("FLOWITH_SSE_HEARTBEAT_INTERVAL", 5.0)
# Fresh Fable turns normally produce their first delta quickly. Bound no-byte
# waits separately so a dead Fable stream is retried instead of pinning a task.
FLOWITH_FABLE_STREAM_IDLE_TIMEOUT = _env_float("FLOWITH_FABLE_STREAM_IDLE_TIMEOUT", 45.0)
FLOWITH_DISABLE_KEEPALIVE = os.environ.get("FLOWITH_DISABLE_KEEPALIVE", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

FLOWITH_TOOL_MODE = os.environ.get("FLOWITH_TOOL_MODE", "xml").strip().lower()
if FLOWITH_TOOL_MODE not in {"xml", "native"}:
    FLOWITH_TOOL_MODE = "xml"

# Floor for the upstream max_tokens budget. Codex often sends a small
# max_tokens that truncates XML tool calls mid-stream; raising the floor
# gives the model room to finish the call. Set to 0 to disable.
FLOWITH_MIN_MAX_TOKENS = _env_int("FLOWITH_MIN_MAX_TOKENS", 8192)

FLOWITH_SSL_VERIFY = os.environ.get("FLOWITH_SSL_VERIFY", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

FLOWITH_TRACE_HERMES = os.environ.get("FLOWITH_TRACE_HERMES", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

# Some Hermes builds render both streaming deltas and every final Responses
# snapshot. Keep final text payloads empty in that compatibility mode.
FLOWITH_RESPONSES_COMPACT_FINAL_TEXT = os.environ.get(
    "FLOWITH_RESPONSES_COMPACT_FINAL_TEXT", "false"
).strip().lower() in {"1", "true", "yes", "on"}

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
# Codex 0.144.x only attaches its local tools to recognized model families.
# This recognized 5.4-prefixed name keeps those tools while the proxy routes to
# the actual Flowith GPT-5.6 model.
DEFAULT_MODEL_ALIASES: dict[str, str] = {
    "gpt-5.4-flowith-5.6": "gpt-5.6-sol",
}
CUSTOM_MODEL_ALIASES: dict[str, str] = dict(DEFAULT_MODEL_ALIASES)
_raw_aliases = os.environ.get("FLOWITH_MODEL_ALIASES", "").strip()
if _raw_aliases:
    try:
        CUSTOM_MODEL_ALIASES.update({
            str(k): str(v) for k, v in json.loads(_raw_aliases).items()
        })
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
