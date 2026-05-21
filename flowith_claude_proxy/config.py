"""Runtime configuration for the Flowith -> Claude proxy."""

from __future__ import annotations

import json
import os
from pathlib import Path

# Project root (one level above this package)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env into os.environ before reading any env vars.
# python-dotenv is optional: if not installed, rely on system env vars only.
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

# Default model used when the client doesn't request anything we recognize.
DEFAULT_MODEL = os.environ.get("FLOWITH_DEFAULT_MODEL", "claude-4.6-sonnet")

# Upstream request timeout (seconds)
API_TIMEOUT = int(os.environ.get("FLOWITH_TIMEOUT", "120"))

# Upstream TLS verification. Set FLOWITH_SSL_VERIFY=false only as a workaround
# when a local proxy/VPN/firewall breaks TLS handshakes.
FLOWITH_SSL_VERIFY = os.environ.get("FLOWITH_SSL_VERIFY", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}

# HTTP/SOCKS proxy for upstream requests (e.g., Clash: http://127.0.0.1:7890)
# Falls back to HTTPS_PROXY / HTTP_PROXY env vars if set.
# Use socks5://127.0.0.1:7891 for SOCKS5 proxies.
_https_proxy = os.environ.get("FLOWITH_UPSTREAM_PROXY") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
_http_proxy = os.environ.get("FLOWITH_UPSTREAM_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
UPSTREAM_PROXIES: dict[str, str] | None = None
if _https_proxy:
    UPSTREAM_PROXIES = {"https": _https_proxy, "http": _http_proxy or _https_proxy}

# Server bind defaults
DEFAULT_HOST = os.environ.get("FLOWITH_API_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("FLOWITH_API_PORT", "8787"))

# Custom model aliases: JSON string, merged with built-in aliases (user ones win).
# e.g. FLOWITH_MODEL_ALIASES={"claude-3-5-sonnet-20241022":"claude-4.6-sonnet"}
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
    """Resolve a Flowith API key.

    Order:
      1. FLOWITH_API_KEY env var (now includes .env values via load_dotenv)
      2. Fallback: manually scan .env in project root or CWD
    """
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
