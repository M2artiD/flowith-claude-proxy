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
    parse_xml_tool_calls,
    split_text_and_xml_tool_calls,
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

    # Parse thinking config from Claude Code request
    raw_thinking = body.get("thinking")
    enable_thinking = isinstance(raw_thinking, dict) and raw_thinking.get("type") == "enabled"

    # Convert tool definitions and tool_choice
    openai_tools = None
    openai_tool_choice = None
    raw_tools = body.get("tools")
    if raw_tools:
        openai_tools = anthropic_tools_to_openai(raw_tools)
        tc = body.get("tool_choice")
        if tc is not None:
            openai_tool_choice = anthropic_tool_choice_to_openai(tc)

    messages = claude_request_to_flowith_messages(body, anthropic_tools=raw_tools)
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
            thinking=enable_thinking,
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

    has_tools = bool(openai_tools)

    return StreamingResponse(
        _stream_claude_events(
            client, messages, requested_model,
            openai_tools=openai_tools,
            openai_tool_choice=openai_tool_choice,
            enable_thinking=enable_thinking,
            has_tools=has_tools,
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
    enable_thinking: bool = False,
    has_tools: bool = False,
) -> Generator[bytes, None, None]:
    q: "queue.Queue[Any]" = queue.Queue(maxsize=512)
    result_holder: dict[str, Any] = {}

    def on_chunk(piece: str) -> None:
        if piece:
            q.put(("text", piece))

    def on_reasoning(piece: str) -> None:
        if piece:
            q.put(("reasoning", piece))

    def on_tool_call(tc: dict[str, Any]) -> None:
        q.put(("tool_call", tc))

    def worker() -> None:
        try:
            res = client.call_api(
                messages,
                max_retries=1,
                stream=True,
                on_chunk=on_chunk,
                on_reasoning=on_reasoning,
                on_tool_call=on_tool_call,
                tools=openai_tools,
                tool_choice=openai_tool_choice,
                thinking=enable_thinking,
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
    current_block_type: str | None = None  # "text", "thinking", or "tool_use"
    streamed_any = False
    last_ping = time.time()

    # XML tool call buffering: accumulate text chunks, detect XML patterns,
    # then convert to tool_use content blocks instead of plain text.
    text_buffer = ""
    xml_parsing = False  # True once we detect <function_calls> start

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
        """Emit a complete text content block for the given text."""
        nonlocal current_block_index, streamed_any
        if not text:
            return
        yield from _close_block()
        yield from _open_block("text")
        streamed_any = True
        yield sse_content_block_delta(text, current_block_index).encode("utf-8")

    def _emit_tool_use_block(tool: dict[str, Any]) -> None:
        """Emit a tool_use content block with progressive input_json_delta."""
        nonlocal current_block_index, streamed_any
        yield from _close_block()
        yield from _open_block(
            "tool_use",
            tool_id=tool["id"],
            tool_name=tool["name"],
        )
        streamed_any = True
        json_str = json.dumps(tool["input"], ensure_ascii=False)
        # Progressive streaming: send in ~30 char chunks to mimic native
        # Anthropic streaming behaviour. Single-chunk for very short JSON.
        _CHUNK = 30
        if len(json_str) <= _CHUNK * 2:
            yield sse_tool_input_delta(json_str, current_block_index).encode("utf-8")
        else:
            for i in range(0, len(json_str), _CHUNK):
                yield sse_tool_input_delta(
                    json_str[i:i + _CHUNK], current_block_index
                ).encode("utf-8")

    def _parse_and_emit_tools(buf: str) -> str:
        """Try to parse XML tool calls from buffer. Returns remaining text
        after the last </function_calls> on success, or the original buffer
        on parse failure (so text is emitted as-is, avoiding a hang)."""
        nonlocal xml_parsing
        try:
            tools = parse_xml_tool_calls(buf)
            if tools:
                for tool in tools:
                    yield from _emit_tool_use_block(tool)
            last_close = buf.rfind("</function_calls>")
            if last_close != -1:
                remaining = buf[last_close + len("</function_calls>"):]
                xml_parsing = "<function_calls>" in remaining
                return remaining
        except Exception:
            # Malformed XML — abandon parsing, emit buffer as plain text
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
            # When no tools are present, forward chunks immediately.
            if not has_tools:
                if current_block_type != "text":
                    yield from _close_block()
                    yield from _open_block("text")
                streamed_any = True
                yield sse_content_block_delta(payload, current_block_index).encode("utf-8")
                continue

            text_buffer += payload

            # Detect XML tool call start
            if "<function_calls>" in text_buffer and not xml_parsing:
                xml_parsing = True
                pre_xml = text_buffer[:text_buffer.index("<function_calls>")]
                if pre_xml.strip():
                    yield from _flush_text_as_block(pre_xml.strip())

            if xml_parsing:
                if "</function_calls>" in text_buffer:
                    text_buffer = yield from _parse_and_emit_tools(text_buffer)
                    # Emit remaining non-XML text
                    if text_buffer and not xml_parsing:
                        if text_buffer.strip():
                            yield from _flush_text_as_block(text_buffer.strip())
                        text_buffer = ""
            else:
                # Keep a trailing window so a <function_calls> tag split
                # across SSE chunks isn't missed. The tag is 16 chars;
                # 48-char window is generous enough for split attributes.
                _TAG_WINDOW = 48
                if len(text_buffer) > _TAG_WINDOW:
                    safe_text = text_buffer[:-_TAG_WINDOW]
                    if current_block_type != "text":
                        yield from _close_block()
                        yield from _open_block("text")
                    streamed_any = True
                    yield sse_content_block_delta(safe_text, current_block_index).encode("utf-8")
                    text_buffer = text_buffer[-_TAG_WINDOW:]

        elif kind == "tool_call":
            # Structured tool call from upstream (native OpenAI tool_calls)
            yield from _close_block()
            tc_id = payload.get("id", f"toolu_{threading.get_ident()}")
            func = payload.get("function", {})
            tc_name = func.get("name", "")
            yield from _open_block("tool_use", tool_id=tc_id, tool_name=tc_name)
            current_block_type = "tool_use"
            streamed_any = True
            raw_args = func.get("arguments", "{}")
            if raw_args:
                # Progressive JSON for native tool calls too
                if len(raw_args) <= 60:
                    yield sse_tool_input_delta(raw_args, current_block_index).encode("utf-8")
                else:
                    for i in range(0, len(raw_args), 30):
                        yield sse_tool_input_delta(
                            raw_args[i:i + 30], current_block_index
                        ).encode("utf-8")

        if time.time() - last_ping > 15:
            yield sse_ping().encode("utf-8")
            last_ping = time.time()

    # Flush any remaining text buffer
    if text_buffer.strip():
        if xml_parsing:
            text_buffer = yield from _parse_and_emit_tools(text_buffer)
        if text_buffer.strip():
            yield from _flush_text_as_block(text_buffer.strip())

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
    # Also check for XML-parsed tool calls in the result content
    result_content = res.get("content", "") or ""
    if not has_tool_calls and "<function_calls>" in result_content:
        has_tool_calls = True
    stop_reason = "tool_use" if has_tool_calls else "end_turn"

    yield sse_message_delta(output_tokens=output_tokens, stop_reason=stop_reason).encode("utf-8")
    yield sse_message_stop().encode("utf-8")
