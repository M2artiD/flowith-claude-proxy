"""FastAPI application: Anthropic-compatible proxy for Flowith."""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from typing import Any, Generator

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
    DEFAULT_MODEL,
    FLOWITH_BASE_URL,
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

_default_client: FlowithClient | None = None


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
    return _SERVER_API_KEY


def _get_client_for_key(api_key: str, flowith_model: str) -> tuple[FlowithClient, str | None]:
    if api_key == _SERVER_API_KEY:
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
    return api_key


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "flowith-claude-proxy",
        "version": __version__,
        "upstream": FLOWITH_BASE_URL,
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


app.include_router(create_codex_router(
    require_profile=_require_profile,
    read_json_object=_read_json_object,
    require_api_key=_require_api_key,
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

    # ===== REQUEST LOG =====
    requested_model = body.get("model", "?")
    tool_count = len(body.get("tools") or [])
    sys_len = len(str(body.get("system") or ""))
    msg_count = len(body.get("messages") or [])
    print(f"[REQ] model={requested_model}  tools={tool_count}  system_len={sys_len}  msgs={msg_count}  stream={body.get('stream')}", flush=True)

    api_key = _resolve_api_key(x_api_key, authorization)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing API key. Send x-api-key or Authorization: Bearer <key>, or set FLOWITH_API_KEY on the server.",
        )

    requested_model = requested_model if requested_model != "?" else DEFAULT_MODEL
    flowith_model = map_model(requested_model, default=DEFAULT_MODEL)
    stream = bool(body.get("stream"))

    raw_thinking = body.get("thinking")
    enable_thinking = isinstance(raw_thinking, dict) and raw_thinking.get("type") == "enabled"
    thinking_budget_tokens = None
    if enable_thinking and isinstance(raw_thinking, dict):
        thinking_budget_tokens = raw_thinking.get("budget_tokens")

    raw_tools = body.get("tools") or []
    # Never pass native OpenAI tools to Flowith — the upstream model may
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

    if api_key == _SERVER_API_KEY:
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
        result = client.call_api(
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
        return JSONResponse(content=flowith_result_to_claude_response(result, requested_model))

    return StreamingResponse(
        _stream_claude_events(
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
        ),
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

    if REACT_TOOL_STOP_SEQUENCE not in sequences:
        sequences.append(REACT_TOOL_STOP_SEQUENCE)
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
            )
            result_holder["result"] = res
        except Exception as e:
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            q.put(_SENTINEL_DONE)

    threading.Thread(target=worker, daemon=True).start()

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
