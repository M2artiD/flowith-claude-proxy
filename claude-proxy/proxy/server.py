"""FastAPI application: Anthropic-compatible proxy for Flowith."""

from __future__ import annotations

import hmac
import json
import ipaddress
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Generator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .adapter import (
    REACT_TOOL_STOP_SEQUENCE,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai,
    claude_request_to_flowith_messages,
    find_xml_tool_call_consumed_end,
    find_xml_tool_call_end,
    find_xml_tool_call_start,
    flowith_result_to_claude_response,
    has_xml_tool_call_marker,
    map_model,
    new_message_id,
    normalize_tool_use_id,
    parse_xml_tool_calls,
    sse_content_block_delta,
    sse_content_block_start,
    sse_content_block_stop,
    sse_error,
    sse_message_delta,
    sse_message_start,
    sse_message_stop,
    sse_ping,
    sse_thinking_delta,
    sse_tool_input_delta,
)
from .codex import create_router as create_codex_router
from .config import (
    API_TIMEOUT,
    CUSTOM_MODEL_ALIASES,
    DEBUG_DUMP,
    DEBUG_DUMP_DIR,
    DEBUG_DUMP_MAX_BYTES,
    DEBUG_DUMP_MAX_FILES,
    DEFAULT_HOST,
    DEFAULT_MODEL,
    DEFAULT_PORT,
    FLOWITH_BASE_URL,
    FLOWITH_CONNECT_TIMEOUT,
    FLOWITH_LOCAL_ONLY,
    FLOWITH_MAX_CONCURRENCY,
    FLOWITH_MAX_REQUEST_BYTES,
    FLOWITH_REQUEST_LOG,
    FLOWITH_REQUIRE_SERVER_KEY,
    FLOWITH_SEMAPHORE_TIMEOUT,
    FLOWITH_SSL_VERIFY,
    FLOWITH_TOOL_MODE,
    UPSTREAM_PROXIES,
    load_api_key,
)
from .upstream import FlowithClient

app = FastAPI(
    title="Flowith Claude-Compatible Proxy",
    version=__version__,
)

_SERVER_API_KEY = load_api_key()

if FLOWITH_REQUIRE_SERVER_KEY and not _SERVER_API_KEY:
    raise RuntimeError(
        "FLOWITH_REQUIRE_SERVER_KEY=true but no server API key is configured. "
        "Set FLOWITH_API_KEY (or write it to .flowith_api_key) before starting the proxy, "
        "or disable FLOWITH_REQUIRE_SERVER_KEY."
    )

if not FLOWITH_LOCAL_ONLY and not FLOWITH_REQUIRE_SERVER_KEY:
    raise RuntimeError(
        "Unsafe proxy configuration: FLOWITH_LOCAL_ONLY=false requires "
        "FLOWITH_REQUIRE_SERVER_KEY=true. Refusing to start an unauthenticated "
        "network-accessible upstream relay."
    )

_default_client: FlowithClient | None = None


def _keys_equal(a: str | None, b: str | None) -> bool:
    """Constant-time comparison; treats a missing side as a mismatch."""
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def _is_local_client_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = host.strip().strip("[]").lower()
    if normalized in {"localhost", "testclient"}:
        return True
    try:
        ip = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(mapped and mapped.is_loopback)


def _request_too_large_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "type": "request_too_large",
                "message": "Request body exceeds FLOWITH_MAX_REQUEST_BYTES.",
            }
        },
    )


async def _request_body_exceeds_limit(request: Request, limit: int) -> bool:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            length = int(content_length)
        except ValueError:
            length = -1
        if length > limit:
            return True

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            return True

    request._body = bytes(body)  # Starlette cache for downstream request.json().
    return False


@app.middleware("http")
async def _local_only_middleware(request: Request, call_next: Any) -> Any:
    if FLOWITH_MAX_REQUEST_BYTES > 0:
        if await _request_body_exceeds_limit(request, FLOWITH_MAX_REQUEST_BYTES):
            return _request_too_large_response()
    if FLOWITH_LOCAL_ONLY:
        client_host = request.client.host if request.client else None
        if not _is_local_client_host(client_host):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "type": "forbidden",
                        "message": "FLOWITH_LOCAL_ONLY is enabled; only localhost clients are allowed.",
                    }
                },
            )
    return await call_next(request)


def _api_profile() -> str:
    profile = os.environ.get("FLOWITH_API_PROFILE", "all").strip().lower()
    return profile if profile in {"all", "claude", "codex", "openai"} else "all"


def _require_profile(*allowed: str) -> None:
    profile = _api_profile()
    if profile != "all" and profile not in allowed:
        raise HTTPException(status_code=404, detail="Endpoint not enabled for this proxy profile")


def _get_default_client() -> FlowithClient:
    global _default_client
    if _default_client is None:
        _default_client = FlowithClient(
            api_key=_SERVER_API_KEY or "",
            model=DEFAULT_MODEL,
            base_url=FLOWITH_BASE_URL,
            timeout=API_TIMEOUT,
            ssl_verify=FLOWITH_SSL_VERIFY,
            proxies=UPSTREAM_PROXIES,
        )
    return _default_client


def _resolve_api_key(
    x_api_key: str | None,
    authorization: str | None,
) -> str | None:
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    # No client-provided key. In server-key-required mode we must NOT silently
    # fall back to the server key here; otherwise the require-server-key gate
    # below (which compares api_key against _SERVER_API_KEY) would always pass
    # for anonymous callers. Only expose the server key implicitly when the
    # gate is off, which preserves the legacy "trusted local caller" behaviour.
    if FLOWITH_REQUIRE_SERVER_KEY:
        return None
    if not FLOWITH_LOCAL_ONLY:
        return None
    return _SERVER_API_KEY


def _get_client_for_key(api_key: str, flowith_model: str) -> tuple[FlowithClient, str | None]:
    if _keys_equal(api_key, _SERVER_API_KEY):
        return _get_default_client(), flowith_model
    return (
        FlowithClient(
            api_key=api_key,
            model=flowith_model,
            base_url=FLOWITH_BASE_URL,
            timeout=API_TIMEOUT,
            ssl_verify=FLOWITH_SSL_VERIFY,
            proxies=UPSTREAM_PROXIES,
        ),
        None,
    )


async def _read_json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    return body


def _require_api_key(x_api_key: str | None, authorization: str | None) -> str:
    api_key = _resolve_api_key(x_api_key, authorization)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Send x-api-key or Authorization: Bearer <key>, or set FLOWITH_API_KEY on the server.",
        )
    if FLOWITH_REQUIRE_SERVER_KEY:
        if not _keys_equal(api_key, _SERVER_API_KEY):
            raise HTTPException(
                status_code=401,
                detail="Invalid API key.",
            )
    return api_key


def _require_discovery_api_key(x_api_key: str | None, authorization: str | None) -> None:
    if FLOWITH_REQUIRE_SERVER_KEY:
        _require_api_key(x_api_key, authorization)


_DASHBOARD_ROUTE_GROUPS = {
    "anthropic": [
        "POST /v1/messages",
    ],
    "openai": [
        "POST /v1/chat/completions",
        "POST /v1/responses",
        "GET /v1/models",
        "GET /models",
    ],
    "diagnostics": [
        "HEAD /",
        "GET /",
        "GET /health",
        "GET /dashboard",
        "GET /dashboard/api/status",
        "GET /dashboard/api/config",
        "GET /dashboard/api/routes",
        "GET /dashboard/api/debug-dumps",
    ],
}


def _dashboard_routes_flat() -> list[str]:
    routes: list[str] = []
    for group_routes in _DASHBOARD_ROUTE_GROUPS.values():
        routes.extend(group_routes)
    return routes


def _mask_secret(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}****{value[-4:]}"


def _available_model_ids() -> list[str]:
    return sorted(set(CUSTOM_MODEL_ALIASES.values()) | {DEFAULT_MODEL})


def _dashboard_auth(x_api_key: str | None, authorization: str | None) -> None:
    if FLOWITH_REQUIRE_SERVER_KEY:
        _require_api_key(x_api_key, authorization)


def _debug_dump_files(limit: int = 50) -> list[dict[str, Any]]:
    dump_dir = Path(DEBUG_DUMP_DIR)
    if not dump_dir.exists() or not dump_dir.is_dir():
        return []
    files: list[tuple[Path, Any]] = []
    for candidate in dump_dir.glob("flowith_*.json"):
        try:
            if not candidate.is_file():
                continue
            stat = candidate.stat()
        except OSError:
            continue
        files.append((candidate, stat))
    files.sort(key=lambda item: (item[1].st_mtime_ns, item[0].name), reverse=True)

    result: list[dict[str, Any]] = []
    for path, stat in files[:limit]:
        result.append(
            {
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified": stat.st_mtime,
            }
        )
    return result


def _debug_dump_count() -> int:
    dump_dir = Path(DEBUG_DUMP_DIR)
    if not dump_dir.exists() or not dump_dir.is_dir():
        return 0
    count = 0
    for candidate in dump_dir.glob("flowith_*.json"):
        try:
            if candidate.is_file():
                count += 1
        except OSError:
            continue
    return count


def _dashboard_status_payload() -> dict[str, Any]:
    model_ids = _available_model_ids()
    return {
        "service": "flowith-claude-proxy",
        "version": __version__,
        "health": {"ok": True},
        "bind": {"host": DEFAULT_HOST, "port": DEFAULT_PORT},
        "upstream": {
            "base_url": FLOWITH_BASE_URL,
            "timeout_seconds": API_TIMEOUT,
            "connect_timeout_seconds": FLOWITH_CONNECT_TIMEOUT,
            "ssl_verify": FLOWITH_SSL_VERIFY,
            "proxy_configured": bool(UPSTREAM_PROXIES),
        },
        "security": {
            "local_only": FLOWITH_LOCAL_ONLY,
            "require_server_key": FLOWITH_REQUIRE_SERVER_KEY,
            "server_key_configured": bool(_SERVER_API_KEY),
        },
        "limits": {
            "max_request_bytes": FLOWITH_MAX_REQUEST_BYTES,
            "max_concurrency": FLOWITH_MAX_CONCURRENCY,
            "semaphore_timeout_seconds": FLOWITH_SEMAPHORE_TIMEOUT,
        },
        "debug_dumps": {
            "enabled": DEBUG_DUMP,
            "dir": DEBUG_DUMP_DIR,
            "max_bytes": DEBUG_DUMP_MAX_BYTES,
            "max_files": DEBUG_DUMP_MAX_FILES,
            "count": _debug_dump_count(),
        },
        "models": {
            "default": DEFAULT_MODEL,
            "available": model_ids,
            "count": len(model_ids),
        },
        "routes": _dashboard_routes_flat(),
    }


def _dashboard_config_payload() -> dict[str, Any]:
    return {
        "FLOWITH_API_KEY": _mask_secret(_SERVER_API_KEY),
        "FLOWITH_BASE_URL": FLOWITH_BASE_URL,
        "FLOWITH_DEFAULT_MODEL": DEFAULT_MODEL,
        "FLOWITH_API_HOST": DEFAULT_HOST,
        "FLOWITH_API_PORT": DEFAULT_PORT,
        "FLOWITH_LOCAL_ONLY": FLOWITH_LOCAL_ONLY,
        "FLOWITH_REQUIRE_SERVER_KEY": FLOWITH_REQUIRE_SERVER_KEY,
        "FLOWITH_MAX_REQUEST_BYTES": FLOWITH_MAX_REQUEST_BYTES,
        "FLOWITH_MAX_CONCURRENCY": FLOWITH_MAX_CONCURRENCY,
        "FLOWITH_SEMAPHORE_TIMEOUT": FLOWITH_SEMAPHORE_TIMEOUT,
        "FLOWITH_TIMEOUT": API_TIMEOUT,
        "FLOWITH_CONNECT_TIMEOUT": FLOWITH_CONNECT_TIMEOUT,
        "FLOWITH_SSL_VERIFY": FLOWITH_SSL_VERIFY,
        "FLOWITH_TOOL_MODE": FLOWITH_TOOL_MODE,
        "FLOWITH_REQUEST_LOG": FLOWITH_REQUEST_LOG,
        "FLOWITH_UPSTREAM_PROXY_CONFIGURED": bool(UPSTREAM_PROXIES),
        "DEBUG_DUMP": DEBUG_DUMP,
        "DEBUG_DUMP_DIR": DEBUG_DUMP_DIR,
        "DEBUG_DUMP_MAX_BYTES": DEBUG_DUMP_MAX_BYTES,
        "DEBUG_DUMP_MAX_FILES": DEBUG_DUMP_MAX_FILES,
        "CUSTOM_MODEL_ALIASES": dict(CUSTOM_MODEL_ALIASES),
    }


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flowith Claude Proxy Console</title>
  <style>
    :root { color-scheme: light dark; font-family: Inter, Segoe UI, Arial, sans-serif; }
    body { margin: 0; padding: 32px; background: #0f172a; color: #e2e8f0; }
    main { max-width: 1100px; margin: 0 auto; }
    h1 { margin: 0 0 8px; font-size: 32px; }
    p { color: #94a3b8; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin: 24px 0; }
    .card { background: #111827; border: 1px solid #334155; border-radius: 14px; padding: 18px; box-shadow: 0 8px 30px #0003; }
    .label { color: #94a3b8; font-size: 13px; text-transform: uppercase; letter-spacing: .06em; }
    .value { font-size: 24px; margin-top: 6px; }
    pre { overflow: auto; white-space: pre-wrap; background: #020617; border-radius: 12px; padding: 16px; border: 1px solid #1e293b; }
    a { color: #7dd3fc; }
    button { background: #2563eb; color: white; border: 0; border-radius: 10px; padding: 10px 14px; cursor: pointer; }
  </style>
</head>
<body>
<main>
  <h1>Flowith Claude Proxy Console</h1>
  <p>Read-only local dashboard for runtime status, safe configuration, route inventory, and debug dump metadata.</p>
  <div class="grid">
    <section class="card"><div class="label">Status</div><div class="value" id="status">Loading?</div></section>
    <section class="card"><div class="label">Bind</div><div class="value" id="bind">?</div></section>
    <section class="card"><div class="label">Local Only</div><div class="value" id="local">?</div></section>
    <section class="card"><div class="label">Models</div><div class="value" id="models">?</div></section>
  </div>
  <p>
    API endpoints:
    <a href="/dashboard/api/status">/dashboard/api/status</a>,
    <a href="/dashboard/api/config">/dashboard/api/config</a>,
    <a href="/dashboard/api/routes">/dashboard/api/routes</a>,
    <a href="/dashboard/api/debug-dumps">/dashboard/api/debug-dumps</a>
  </p>
  <button id="refresh">Refresh</button>
  <h2>Status JSON</h2>
  <pre id="json">Loading?</pre>
</main>
<script>
async function loadStatus() {
  const res = await fetch('/dashboard/api/status');
  const data = await res.json();
  document.getElementById('status').textContent = data.health && data.health.ok ? 'Running' : 'Issue';
  document.getElementById('bind').textContent = `${data.bind.host}:${data.bind.port}`;
  document.getElementById('local').textContent = String(data.security.local_only);
  document.getElementById('models').textContent = String(data.models.count);
  document.getElementById('json').textContent = JSON.stringify(data, null, 2);
}
document.getElementById('refresh').addEventListener('click', loadStatus);
loadStatus().catch(err => { document.getElementById('json').textContent = String(err); });
</script>
</body>
</html>
"""


@app.head("/")
def root_head() -> Response:
    return Response(status_code=200)


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "flowith-claude-proxy",
        "version": __version__,
        "endpoints": [
            "POST /v1/messages",
            "POST /v1/chat/completions",
            "POST /v1/responses",
            "GET /v1/models",
        ],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> HTMLResponse:
    _dashboard_auth(x_api_key, authorization)
    return HTMLResponse(_DASHBOARD_HTML)


@app.get("/dashboard/api/status")
def dashboard_status(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _dashboard_auth(x_api_key, authorization)
    return _dashboard_status_payload()


@app.get("/dashboard/api/config")
def dashboard_config(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _dashboard_auth(x_api_key, authorization)
    return _dashboard_config_payload()


@app.get("/dashboard/api/routes")
def dashboard_routes(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> dict[str, list[str]]:
    _dashboard_auth(x_api_key, authorization)
    return {group: list(routes) for group, routes in _DASHBOARD_ROUTE_GROUPS.items()}


@app.get("/dashboard/api/debug-dumps")
def dashboard_debug_dumps(
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _dashboard_auth(x_api_key, authorization)
    return {
        "enabled": DEBUG_DUMP,
        "dir": DEBUG_DUMP_DIR,
        "max_bytes": DEBUG_DUMP_MAX_BYTES,
        "max_files": DEBUG_DUMP_MAX_FILES,
        "count": _debug_dump_count(),
        "files": _debug_dump_files(limit=50),
    }


app.include_router(create_codex_router(
    require_profile=_require_profile,
    read_json_object=_read_json_object,
    require_api_key=_require_api_key,
    require_discovery_api_key=_require_discovery_api_key,
    get_client_for_key=_get_client_for_key,
    with_xml_tool_stop_sequence=lambda stop_sequences: _with_xml_tool_stop_sequence(stop_sequences),
))


@app.post("/v1/messages")
async def create_message(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> Any:
    _require_profile("claude")
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")

    requested_model = body.get("model", "?")
    if FLOWITH_REQUEST_LOG:
        tool_count = len(body.get("tools") or [])
        sys_len = len(str(body.get("system") or ""))
        msg_count = len(body.get("messages") or [])
        print(
            f"[REQ] model={requested_model}  tools={tool_count}  "
            f"system_len={sys_len}  msgs={msg_count}  stream={body.get('stream')}",
            flush=True,
        )

    api_key = _require_api_key(x_api_key, authorization)

    requested_model = requested_model if requested_model != "?" else DEFAULT_MODEL
    flowith_model = map_model(requested_model, default=DEFAULT_MODEL)
    stream = bool(body.get("stream"))

    raw_thinking = body.get("thinking")
    enable_thinking = isinstance(raw_thinking, dict) and raw_thinking.get("type") == "enabled"
    thinking_budget_tokens = None
    if enable_thinking and isinstance(raw_thinking, dict):
        thinking_budget_tokens = raw_thinking.get("budget_tokens")

    raw_tools = body.get("tools") or []
    # Never pass native OpenAI tools to Flowith; the upstream model may
    # either reject them (claude-fable-5) or get confused by the dual
    # native+XML instruction.  Tool guidance is injected exclusively via
    # the system-prompt XML block in claude_request_to_flowith_messages().
    has_tools = bool(raw_tools)
    tool_mode = FLOWITH_TOOL_MODE if has_tools else "xml"
    native_tools = anthropic_tools_to_openai(raw_tools) if tool_mode == "native" else None
    native_tool_choice = (
        anthropic_tool_choice_to_openai(body.get("tool_choice"))
        if native_tools
        else None
    )

    messages = claude_request_to_flowith_messages(
        body,
        anthropic_tools=raw_tools if has_tools and tool_mode == "xml" else None,
        tool_mode=tool_mode,
        tool_choice=body.get("tool_choice"),
    )
    if not messages or all(m["role"] == "system" for m in messages):
        raise HTTPException(status_code=400, detail="At least one user/assistant message is required")

    if _keys_equal(api_key, _SERVER_API_KEY):
        client = _get_default_client()
    else:
        client = FlowithClient(
            api_key=api_key,
            model=flowith_model,
            base_url=FLOWITH_BASE_URL,
            timeout=API_TIMEOUT,
            ssl_verify=FLOWITH_SSL_VERIFY,
            proxies=UPSTREAM_PROXIES,
        )
        flowith_model = None

    max_tokens = body.get("max_tokens")
    temperature = body.get("temperature")
    top_p = body.get("top_p")
    stop_sequences = body.get("stop_sequences")
    if has_tools and tool_mode == "xml":
        stop_sequences = _with_xml_tool_stop_sequence(stop_sequences)

    if not stream:
        result = await run_in_threadpool(
            client.call_api,
            messages,
            model=flowith_model,
            max_retries=2,
            stream=False,
            tools=native_tools,
            tool_choice=native_tool_choice,
            thinking=enable_thinking,
            thinking_budget_tokens=thinking_budget_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop_sequences=stop_sequences,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=502,
                content={
                    "type": "error",
                    "error": {
                        "type": "upstream_error",
                        "message": str(result.get("error", "unknown upstream error")),
                    },
                },
            )
        if _looks_like_hook_json_request(body, messages):
            result = _normalise_hook_json_result(result)
        return JSONResponse(content=flowith_result_to_claude_response(result, requested_model))

    if _looks_like_hook_json_request(body, messages):
        event_stream = _stream_hook_json_events(
            client, messages, requested_model, flowith_model,
            enable_thinking=enable_thinking,
            thinking_budget_tokens=thinking_budget_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop_sequences=stop_sequences,
        )
    else:
        event_stream = _stream_claude_events(
            client, messages, requested_model, flowith_model,
            enable_thinking=enable_thinking,
            has_tools=has_tools,
            native_tools=native_tools,
            native_tool_choice=native_tool_choice,
            thinking_budget_tokens=thinking_budget_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop_sequences=stop_sequences,
        )

    return StreamingResponse(
        event_stream,
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_SENTINEL_DONE = object()
_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


def _looks_like_hook_json_request(body: dict[str, Any], messages: list[dict[str, Any]]) -> bool:
    """Detect strict Claude Code hook-validation requests without catching normal goal chat."""
    if body.get("tools"):
        return False

    chunks: list[str] = []
    for field in ("system", "metadata"):
        value = body.get(field)
        if value:
            chunks.append(str(value))

    last_message = messages[-1] if messages else {}
    last_content = str(last_message.get("content", "") or "")
    joined = "\n".join(chunks + [last_content]).lower()

    has_json_keys = '"ok"' in joined and '"reason"' in joined
    has_json_contract = any(marker in joined for marker in (
        "json object",
        "valid json",
        "must return json",
        "respond with json",
        "response must be json",
        "output json",
        "strict json",
    ))
    has_explicit_hook_context = any(marker in joined for marker in (
        "stop hook",
        "subagentstop",
        "hook event",
        "hook result",
        "session-scoped stop hook",
        "claude code hook",
        "stop-condition hook",
        "stop condition hook",
    ))

    return has_json_keys and has_json_contract and has_explicit_hook_context


def _extract_first_json_object(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for pos in range(start, len(text)):
            ch = text[pos]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:pos + 1]
                    try:
                        parsed = json.loads(candidate)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        break
                    return parsed if isinstance(parsed, dict) else None
        start = text.find("{", start + 1)
    return None


def _normalise_hook_json_result(result: dict[str, Any]) -> dict[str, Any]:
    content = str(result.get("content", "") or "").strip()
    if not content:
        normalised = dict(result)
        normalised["content"] = json.dumps(
            {"ok": False, "reason": "Upstream returned empty content for the hook JSON check."},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return normalised

    parsed = _extract_first_json_object(content)
    if isinstance(parsed, dict) and isinstance(parsed.get("ok"), bool):
        ok = bool(parsed["ok"])
        reason = str(parsed.get("reason", "") or content).strip()
    else:
        lowered = content.lower()
        negative_markers = (
            "not met",
            "not satisfied",
            "incomplete",
            "continue",
            "keep working",
            "missing",
            "blocked",
            "cannot",
            "can't",
            "failed",
            '"ok": false',
            '"ok":false',
        )
        positive_markers = (
            "completed",
            "done",
            "satisfied",
            "success",
            "passed",
            "ready",
            '"ok": true',
            '"ok":true',
        )

        if any(marker in lowered for marker in negative_markers):
            ok = False
        elif any(marker in lowered for marker in positive_markers):
            ok = True
        else:
            ok = False

        reason = content

    normalised = dict(result)
    normalised["content"] = json.dumps(
        {"ok": ok, "reason": reason},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return normalised


def _stream_hook_json_events(
    client: FlowithClient,
    messages: list[dict[str, Any]],
    requested_model: str,
    upstream_model: str | None = None,
    enable_thinking: bool = False,
    thinking_budget_tokens: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop_sequences: list[str] | str | None = None,
) -> Generator[bytes, None, None]:
    message_id = new_message_id()
    yield sse_message_start(message_id, requested_model, input_tokens=0).encode("utf-8")
    yield sse_ping().encode("utf-8")

    try:
        result = client.call_api(
            messages,
            model=upstream_model,
            max_retries=2,
            stream=False,
            tools=None,
            tool_choice=None,
            thinking=enable_thinking,
            thinking_budget_tokens=thinking_budget_tokens,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop_sequences=stop_sequences,
        )
    except Exception as e:
        result = {"success": False, "error": str(e)}

    if not result.get("success"):
        err_msg = str(result.get("error", "upstream error"))
        yield sse_content_block_start(0, block_type="text").encode("utf-8")
        yield sse_content_block_delta(
            json.dumps(
                {"ok": False, "reason": f"Hook JSON check failed: {err_msg}"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            0,
        ).encode("utf-8")
        yield sse_content_block_stop(0).encode("utf-8")
        yield sse_message_delta(output_tokens=0, stop_reason="end_turn").encode("utf-8")
        yield sse_message_stop().encode("utf-8")
        return

    normalised = _normalise_hook_json_result(result)
    text = str(normalised.get("content", "") or "")
    usage = normalised.get("usage", {}) or {}
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    yield sse_content_block_start(0, block_type="text").encode("utf-8")
    yield sse_content_block_delta(text, 0).encode("utf-8")
    yield sse_content_block_stop(0).encode("utf-8")
    yield sse_message_delta(output_tokens=output_tokens, stop_reason="end_turn").encode("utf-8")
    yield sse_message_stop().encode("utf-8")


def _filter_stream_think_tags(raw: str, in_think: bool) -> tuple[str, str, bool]:
    clean_parts: list[str] = []
    pos = 0

    while pos < len(raw):
        if in_think:
            close_idx = raw.find(_THINK_CLOSE_TAG, pos)
            if close_idx >= 0:
                pos = close_idx + len(_THINK_CLOSE_TAG)
                in_think = False
                continue

            remainder = raw[pos:]
            for i in range(min(len(_THINK_CLOSE_TAG) - 1, len(remainder)), 0, -1):
                if remainder.endswith(_THINK_CLOSE_TAG[:i]):
                    return "".join(clean_parts), remainder[-i:], True
            return "".join(clean_parts), "", True

        open_idx = raw.find(_THINK_OPEN_TAG, pos)
        if open_idx >= 0:
            clean_parts.append(raw[pos:open_idx])
            pos = open_idx + len(_THINK_OPEN_TAG)
            in_think = True
            continue

        remainder = raw[pos:]
        for i in range(min(len(_THINK_OPEN_TAG) - 1, len(remainder)), 0, -1):
            if remainder.endswith(_THINK_OPEN_TAG[:i]):
                clean_parts.append(remainder[:-i])
                return "".join(clean_parts), remainder[-i:], False
        clean_parts.append(remainder)
        return "".join(clean_parts), "", False

    return "".join(clean_parts), "", in_think


def _with_xml_tool_stop_sequence(
    stop_sequences: list[str] | str | None,
) -> list[str]:
    if stop_sequences is None:
        sequences: list[str] = []
    elif isinstance(stop_sequences, str):
        sequences = [stop_sequences]
    else:
        sequences = list(stop_sequences)

    # Do not inject </tool_call> as an upstream stop sequence. Some upstream
    # providers match stop strings against the full prompt/transcript, so a
    # previous assistant tool call in conversation history can terminate the
    # next response before any new tokens are produced. Streaming/non-streaming
    # parsers already tolerate both complete and stop-truncated tool XML.
    return sequences


def _stream_claude_events(
    client: FlowithClient,
    messages: list[dict[str, Any]],
    requested_model: str,
    upstream_model: str | None = None,
    enable_thinking: bool = False,
    has_tools: bool = False,
    native_tools: list[dict[str, Any]] | None = None,
    native_tool_choice: Any = None,
    thinking_budget_tokens: int | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    stop_sequences: list[str] | str | None = None,
) -> Generator[bytes, None, None]:
    q: "queue.Queue[Any]" = queue.Queue(maxsize=512)
    result_holder: dict[str, Any] = {}
    cancel_event = threading.Event()

    def _safe_put(item: Any) -> None:
        # Drop chunks once the client disconnected so the worker cannot
        # wedge on a full queue and leak the upstream semaphore.
        while not cancel_event.is_set():
            try:
                q.put(item, timeout=1)
                return
            except queue.Full:
                continue

    def on_chunk(piece: str) -> None:
        if piece:
            _safe_put(("text", piece))

    def on_reasoning(piece: str) -> None:
        if piece:
            _safe_put(("reasoning", piece))

    def on_tool_call(tc: dict[str, Any]) -> None:
        _safe_put(("tool_call", tc))

    def worker() -> None:
        try:
            res = client.call_api(
                messages,
                model=upstream_model,
                max_retries=1,
                stream=True,
                on_chunk=on_chunk,
                on_reasoning=on_reasoning,
                on_tool_call=on_tool_call,
                tools=native_tools,
                tool_choice=native_tool_choice,
                thinking=enable_thinking,
                thinking_budget_tokens=thinking_budget_tokens,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop_sequences=stop_sequences,
                cancel_event=cancel_event,
            )
            result_holder["result"] = res
        except Exception as e:
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            _safe_put(_SENTINEL_DONE)

    threading.Thread(target=worker, daemon=True).start()

    try:
        message_id = new_message_id()
        yield sse_message_start(message_id, requested_model, input_tokens=0).encode("utf-8")
        yield sse_ping().encode("utf-8")

        current_block_index = 0
        current_block_type: str | None = None
        streamed_any = False
        emitted_tool_use = False
        last_ping = time.time()

        text_buffer = ""
        xml_parsing = False
        think_tail = ""
        in_think_text = False

        def _close_block() -> None:
            nonlocal current_block_type, current_block_index
            if current_block_type is not None:
                yield sse_content_block_stop(current_block_index).encode("utf-8")
                current_block_index += 1
                current_block_type = None

        def _open_block(btype: str, **kwargs: Any) -> None:
            nonlocal current_block_type
            yield sse_content_block_start(current_block_index, block_type=btype, **kwargs).encode("utf-8")
            current_block_type = btype

        def _flush_text_as_block(text: str) -> None:
            nonlocal current_block_index, streamed_any
            if not text:
                return
            yield from _close_block()
            yield from _open_block("text")
            streamed_any = True
            yield sse_content_block_delta(text, current_block_index).encode("utf-8")

        def _emit_tool_use_block(tool: dict[str, Any]) -> None:
            nonlocal current_block_index, streamed_any, emitted_tool_use
            yield from _close_block()
            yield from _open_block("tool_use", tool_id=tool["id"], tool_name=tool["name"])
            streamed_any = True
            emitted_tool_use = True
            json_str = json.dumps(tool["input"], ensure_ascii=False)
            _CHUNK = 30
            if len(json_str) <= _CHUNK * 2:
                yield sse_tool_input_delta(json_str, current_block_index).encode("utf-8")
            else:
                for i in range(0, len(json_str), _CHUNK):
                    yield sse_tool_input_delta(json_str[i:i + _CHUNK], current_block_index).encode("utf-8")

        def _parse_and_emit_tools(buf: str) -> str:
            nonlocal xml_parsing
            try:
                tools = parse_xml_tool_calls(buf)
                if tools:
                    for tool in tools:
                        yield from _emit_tool_use_block(tool)
                consumed_end = find_xml_tool_call_consumed_end(buf)
                if consumed_end == -1:
                    consumed_end = find_xml_tool_call_end(buf)
                if consumed_end != -1:
                    remaining = buf[consumed_end:]
                    xml_parsing = has_xml_tool_call_marker(remaining)
                    return remaining
            except Exception:
                pass
            xml_parsing = False
            return buf

        while True:
            try:
                item = q.get(timeout=10)
            except queue.Empty:
                yield sse_ping().encode("utf-8")
                last_ping = time.time()
                continue

            if item is _SENTINEL_DONE:
                break

            if not isinstance(item, tuple) or len(item) != 2:
                continue

            kind, payload = item

            if kind == "reasoning":
                if current_block_type != "thinking":
                    yield from _close_block()
                    yield from _open_block("thinking")
                streamed_any = True
                yield sse_thinking_delta(payload, current_block_index).encode("utf-8")

            elif kind == "text":
                think_tail += payload
                payload, think_tail, in_think_text = _filter_stream_think_tags(
                    think_tail,
                    in_think_text,
                )
                if not payload:
                    continue

                if not has_tools:
                    if current_block_type != "text":
                        yield from _close_block()
                        yield from _open_block("text")
                    streamed_any = True
                    yield sse_content_block_delta(payload, current_block_index).encode("utf-8")
                    continue

                text_buffer += payload

                if not xml_parsing:
                    tag_start = find_xml_tool_call_start(text_buffer)

                    if tag_start != -1:
                        xml_parsing = True
                        pre_xml = text_buffer[:tag_start]
                        if pre_xml:
                            yield from _flush_text_as_block(pre_xml)

                if xml_parsing:
                    has_close = find_xml_tool_call_end(text_buffer) != -1
                    if has_close:
                        text_buffer = yield from _parse_and_emit_tools(text_buffer)
                        if text_buffer and not xml_parsing:
                            yield from _flush_text_as_block(text_buffer)
                            text_buffer = ""
                else:
                    _TAG_WINDOW = 128
                    if len(text_buffer) > _TAG_WINDOW:
                        safe_text = text_buffer[:-_TAG_WINDOW]
                        if current_block_type != "text":
                            yield from _close_block()
                            yield from _open_block("text")
                        streamed_any = True
                        yield sse_content_block_delta(safe_text, current_block_index).encode("utf-8")
                        text_buffer = text_buffer[-_TAG_WINDOW:]

            elif kind == "tool_call":
                yield from _close_block()
                tc_id = normalize_tool_use_id(payload.get("id", f"toolu_{threading.get_ident()}"))
                func = payload.get("function", {})
                tc_name = func.get("name", "")
                yield from _open_block("tool_use", tool_id=tc_id, tool_name=tc_name)
                current_block_type = "tool_use"
                streamed_any = True
                emitted_tool_use = True
                raw_args = func.get("arguments", "{}")
                if raw_args:
                    if len(raw_args) <= 60:
                        yield sse_tool_input_delta(raw_args, current_block_index).encode("utf-8")
                    else:
                        for i in range(0, len(raw_args), 30):
                            yield sse_tool_input_delta(raw_args[i:i + 30], current_block_index).encode("utf-8")

            if time.time() - last_ping > 15:
                yield sse_ping().encode("utf-8")
                last_ping = time.time()

        if think_tail and not in_think_text:
            if not has_tools:
                if current_block_type != "text":
                    yield from _close_block()
                    yield from _open_block("text")
                streamed_any = True
                yield sse_content_block_delta(think_tail, current_block_index).encode("utf-8")
            else:
                text_buffer += think_tail
            think_tail = ""

        if text_buffer:
            if xml_parsing:
                text_buffer = yield from _parse_and_emit_tools(text_buffer)
            if text_buffer:
                yield from _flush_text_as_block(text_buffer)

        if current_block_type is not None:
            yield from _close_block()

        res = result_holder.get("result") or {}
        if not res.get("success"):
            err_msg = str(res.get("error", "upstream error"))
            if "Upstream stream ended without content" in err_msg and not streamed_any:
                # Empty upstream SSE after retries is a transient provider quirk.
                # Do not surface it as an Anthropic API error because Claude Code
                # treats stream error events as hard failures; finish the turn
                # cleanly instead.
                yield sse_message_delta(output_tokens=0, stop_reason="end_turn").encode("utf-8")
                yield sse_message_stop().encode("utf-8")
                return
            if not streamed_any:
                yield sse_content_block_start(0, block_type="text").encode("utf-8")
                yield sse_content_block_delta(f"[upstream error] {err_msg}", 0).encode("utf-8")
                yield sse_content_block_stop(0).encode("utf-8")
            yield sse_error(err_msg).encode("utf-8")
            yield sse_message_stop().encode("utf-8")
            return

        usage = res.get("usage", {}) or {}
        output_tokens = int(usage.get("completion_tokens", 0) or 0)
        finish_reason = res.get("finish_reason") or ""

        has_tool_calls = emitted_tool_use or bool(res.get("tool_calls"))
        if not has_tool_calls:
            result_content = res.get("content", "") or ""
            has_tool_calls = has_xml_tool_call_marker(result_content)

        if has_tool_calls:
            stop_reason = "tool_use"
        elif finish_reason == "length":
            stop_reason = "max_tokens"
        else:
            stop_reason = "end_turn"

        yield sse_message_delta(output_tokens=output_tokens, stop_reason=stop_reason).encode("utf-8")
        yield sse_message_stop().encode("utf-8")
    finally:
        cancel_event.set()
