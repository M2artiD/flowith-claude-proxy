"""FastAPI application: Anthropic-compatible proxy for Flowith."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Generator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .adapter import (
    claude_request_to_flowith_messages,
    flowith_result_to_claude_response,
    map_model,
    new_message_id,
    sse_content_block_delta,
    sse_content_block_start,
    sse_content_block_stop,
    sse_error,
    sse_message_delta,
    sse_message_start,
    sse_message_stop,
    sse_ping,
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

    messages = claude_request_to_flowith_messages(body)
    if not messages or all(m["role"] == "system" for m in messages):
        raise HTTPException(
            status_code=400,
            detail="At least one user/assistant message is required",
        )

    client = FlowithClient(
        api_key=api_key,
        model=flowith_model,
        base_url=FLOWITH_BASE_URL,
        timeout=API_TIMEOUT,
        ssl_verify=FLOWITH_SSL_VERIFY,
        proxies=UPSTREAM_PROXIES,
    )

    if not stream:
        result = client.call_api(messages, max_retries=2, stream=False)
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
        _stream_claude_events(client, messages, requested_model),
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
) -> Generator[bytes, None, None]:
    q: "queue.Queue[Any]" = queue.Queue(maxsize=256)
    result_holder: dict[str, Any] = {}

    def on_chunk(piece: str) -> None:
        if piece:
            q.put(piece)

    def worker() -> None:
        try:
            res = client.call_api(
                messages, max_retries=1, stream=True, on_chunk=on_chunk
            )
            result_holder["result"] = res
        except Exception as e:  # pragma: no cover
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            q.put(_SENTINEL_DONE)

    threading.Thread(target=worker, daemon=True).start()

    message_id = new_message_id()
    # Anthropic-compatible event order:
    # message_start -> ping -> content_block_start -> delta*
    #   -> content_block_stop -> message_delta -> message_stop
    yield sse_message_start(message_id, requested_model, input_tokens=0).encode("utf-8")
    yield sse_ping().encode("utf-8")
    yield sse_content_block_start(0).encode("utf-8")

    last_ping = time.time()
    streamed_any = False

    while True:
        try:
            item = q.get(timeout=10)
        except queue.Empty:
            yield sse_ping().encode("utf-8")
            last_ping = time.time()
            continue

        if item is _SENTINEL_DONE:
            break

        if isinstance(item, str):
            streamed_any = True
            yield sse_content_block_delta(item, 0).encode("utf-8")
            if time.time() - last_ping > 15:
                yield sse_ping().encode("utf-8")
                last_ping = time.time()

    res = result_holder.get("result") or {}
    if not res.get("success"):
        err_msg = str(res.get("error", "upstream error"))
        if not streamed_any:
            yield sse_content_block_delta(
                f"[upstream error] {err_msg}", 0
            ).encode("utf-8")
        yield sse_content_block_stop(0).encode("utf-8")
        yield sse_error(err_msg).encode("utf-8")
        yield sse_message_stop().encode("utf-8")
        return

    usage = res.get("usage", {}) or {}
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    yield sse_content_block_stop(0).encode("utf-8")
    yield sse_message_delta(output_tokens=output_tokens).encode("utf-8")
    yield sse_message_stop().encode("utf-8")
