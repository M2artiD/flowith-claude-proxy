"""FastAPI application: Anthropic-compatible proxy for Flowith."""

from __future__ import annotations

import json
import queue
import threading
import time
from typing import Any, Generator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .adapter import (
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai,
    claude_request_to_flowith_messages,
    flowith_result_to_claude_response,
    map_model,
    new_message_id,
    openai_tool_calls_to_anthropic,
    sse_content_block_delta,
    sse_content_block_start,
    sse_content_block_stop,
    sse_error,
    sse_message_delta,
    sse_message_start,
    sse_message_stop,
    sse_ping,
    sse_tool_input_delta,
)
from .config import (
    API_TIMEOUT,
    DEFAULT_MODEL,
    FLOWITH_BASE_URL,
    FLOWITH_SSL_VERIFY,
    UPSTREAM_PROXIES,
    load_api_key,
)
from .flowith_client import FlowithClient

app = FastAPI(
    title="Flowith Claude-Compatible Proxy",
    version=__version__,
)

_SERVER_API_KEY = load_api_key()

# Reusable client for server-side API key requests (connection pooling)
_default_client: FlowithClient | None = None


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
    return _SERVER_API_KEY


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "flowith-claude-proxy",
        "version": __version__,
        "upstream": FLOWITH_BASE_URL,
        "endpoints": ["POST /v1/messages"],
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.post("/v1/messages")
async def create_message(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="x-api-key"),
    authorization: str | None = Header(default=None),
) -> Any:
    try:
        body = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="Request body must be a JSON object"
        )

    api_key = _resolve_api_key(x_api_key, authorization)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail=(
                "Missing API key. Send x-api-key or "
                "Authorization: Bearer <key>, or set FLOWITH_API_KEY on the server."
            ),
        )

    requested_model = body.get("model") or DEFAULT_MODEL
    flowith_model = map_model(requested_model, default=DEFAULT_MODEL)
    stream = bool(body.get("stream"))

    # Convert tool definitions and tool_choice
    openai_tools = None
    openai_tool_choice = None
    raw_tools = body.get("tools")
    if raw_tools:
        openai_tools = anthropic_tools_to_openai(raw_tools)
        tc = body.get("tool_choice")
        if tc is not None:
            openai_tool_choice = anthropic_tool_choice_to_openai(tc)

    messages = claude_request_to_flowith_messages(body)
    if not messages or all(m["role"] == "system" for m in messages):
        raise HTTPException(
            status_code=400,
            detail="At least one user/assistant message is required",
        )

    # Reuse pooled client when using server API key; create fresh one for per-request keys
    if api_key == _SERVER_API_KEY:
        client = _get_default_client()
        client.model = flowith_model
    else:
        client = FlowithClient(
            api_key=api_key,
            model=flowith_model,
            base_url=FLOWITH_BASE_URL,
            timeout=API_TIMEOUT,
            ssl_verify=FLOWITH_SSL_VERIFY,
            proxies=UPSTREAM_PROXIES,
        )

    if not stream:
        result = client.call_api(
            messages,
            max_retries=2,
            stream=False,
            tools=openai_tools,
            tool_choice=openai_tool_choice,
        )
        if not result.get("success"):
            return JSONResponse(
                status_code=502,
                content={
                    "type": "error",
                    "error": {
                        "type": "upstream_error",
                        "message": str(
                            result.get("error", "unknown upstream error")
                        ),
                    },
                },
            )
        return JSONResponse(
            content=flowith_result_to_claude_response(result, requested_model)
        )

    return StreamingResponse(
        _stream_claude_events(
            client, messages, requested_model,
            openai_tools=openai_tools,
            openai_tool_choice=openai_tool_choice,
        ),
        media_type="text/event-stream; charset=utf-8",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


_SENTINEL_DONE = object()


def _stream_claude_events(
    client: FlowithClient,
    messages: list[dict[str, Any]],
    requested_model: str,
    openai_tools: list[dict[str, Any]] | None = None,
    openai_tool_choice: Any = None,
) -> Generator[bytes, None, None]:
    q: "queue.Queue[Any]" = queue.Queue(maxsize=512)
    result_holder: dict[str, Any] = {}

    def on_chunk(piece: str) -> None:
        if piece:
            q.put(("text", piece))

    def on_tool_call(tc: dict[str, Any]) -> None:
        q.put(("tool_call", tc))

    def worker() -> None:
        try:
            res = client.call_api(
                messages,
                max_retries=1,
                stream=True,
                on_chunk=on_chunk,
                on_tool_call=on_tool_call,
                tools=openai_tools,
                tool_choice=openai_tool_choice,
            )
            result_holder["result"] = res
        except Exception as e:  # pragma: no cover
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            q.put(_SENTINEL_DONE)

    threading.Thread(target=worker, daemon=True).start()

    message_id = new_message_id()
    yield sse_message_start(message_id, requested_model, input_tokens=0).encode("utf-8")
    yield sse_ping().encode("utf-8")

    # Track content block state
    current_block_index = 0
    current_block_type: str | None = None  # "text" or "tool_use"
    streamed_any = False
    last_ping = time.time()

    def close_current_block() -> None:
        nonlocal current_block_type
        if current_block_type is not None:
            yield sse_content_block_stop(current_block_index).encode("utf-8")
            current_block_index + 1  # advance for next block
            current_block_type = None

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

        if kind == "text":
            # Open text block if not open or if current is tool_use
            if current_block_type != "text":
                if current_block_type is not None:
                    yield sse_content_block_stop(current_block_index).encode("utf-8")
                    current_block_index += 1
                yield sse_content_block_start(current_block_index, block_type="text").encode("utf-8")
                current_block_type = "text"
            streamed_any = True
            yield sse_content_block_delta(payload, current_block_index).encode("utf-8")

        elif kind == "tool_call":
            # Close current block if any
            if current_block_type is not None:
                yield sse_content_block_stop(current_block_index).encode("utf-8")
                current_block_index += 1

            # Open tool_use block
            tc_id = payload.get("id", f"toolu_{threading.get_ident()}")
            func = payload.get("function", {})
            tc_name = func.get("name", "")
            yield sse_content_block_start(
                current_block_index, block_type="tool_use",
                tool_id=tc_id, tool_name=tc_name,
            ).encode("utf-8")
            current_block_type = "tool_use"
            streamed_any = True

            # Stream the arguments as input_json_delta
            raw_args = func.get("arguments", "{}")
            if raw_args:
                yield sse_tool_input_delta(raw_args, current_block_index).encode("utf-8")

        if time.time() - last_ping > 15:
            yield sse_ping().encode("utf-8")
            last_ping = time.time()

    # Close last content block
    if current_block_type is not None:
        yield sse_content_block_stop(current_block_index).encode("utf-8")

    res = result_holder.get("result") or {}
    if not res.get("success"):
        err_msg = str(res.get("error", "upstream error"))
        if not streamed_any:
            yield sse_content_block_start(0, block_type="text").encode("utf-8")
            yield sse_content_block_delta(
                f"[upstream error] {err_msg}", 0
            ).encode("utf-8")
            yield sse_content_block_stop(0).encode("utf-8")
        yield sse_error(err_msg).encode("utf-8")
        yield sse_message_stop().encode("utf-8")
        return

    usage = res.get("usage", {}) or {}
    output_tokens = int(usage.get("completion_tokens", 0) or 0)
    has_tool_calls = bool(res.get("tool_calls"))
    stop_reason = "tool_use" if has_tool_calls else "end_turn"

    yield sse_message_delta(output_tokens=output_tokens, stop_reason=stop_reason).encode("utf-8")
    yield sse_message_stop().encode("utf-8")
