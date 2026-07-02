"""Codex/OpenAI-compatible routes for the Flowith proxy."""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Generator

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from ..adapter import (
    build_tool_xml_prompt,
    find_xml_tool_call_consumed_end,
    find_xml_tool_call_end,
    find_xml_tool_call_start,
    format_observation_xml,
    format_tool_call_xml,
    map_model,
    new_message_id,
    parse_xml_tool_calls,
)
from .. import __version__
from ..config import DEFAULT_MODEL, FLOWITH_TRACE_HERMES
from ..upstream import FlowithClient


_TRACE_HERMES = FLOWITH_TRACE_HERMES


def _trace_hermes(message: str) -> None:
    if _TRACE_HERMES:
        print(f"[HERMES-TRACE] {message}", flush=True)

_SENTINEL_DONE = object()

ReadJsonObject = Callable[[Request], Awaitable[dict[str, Any]]]
RequireProfile = Callable[..., None]
RequireApiKey = Callable[[str | None, str | None], str]
GetClientForKey = Callable[[str, str], tuple[FlowithClient, str | None]]
WithXmlToolStopSequence = Callable[[list[str] | str | None], list[str]]


def create_router(
    *,
    require_profile: RequireProfile,
    read_json_object: ReadJsonObject,
    require_api_key: RequireApiKey,
    get_client_for_key: GetClientForKey,
    with_xml_tool_stop_sequence: WithXmlToolStopSequence,
) -> APIRouter:
    """Build the Codex/OpenAI router while reusing server-owned auth/client state."""

    router = APIRouter()

    @router.get("/v1/models")
    def list_models() -> dict[str, Any]:
        require_profile("codex", "openai")
        from ..config import CUSTOM_MODEL_ALIASES

        model_ids = sorted(set(CUSTOM_MODEL_ALIASES.values()) | {DEFAULT_MODEL})
        return {
            "object": "list",
            "data": [
                {"id": model_id, "object": "model", "created": 0, "owned_by": "flowith"}
                for model_id in model_ids
            ],
        }

    @router.get("/api/v1/models")
    def list_api_v1_models() -> dict[str, Any]:
        return list_models()

    @router.get("/api/tags")
    def list_ollama_tags() -> dict[str, Any]:
        require_profile("codex", "openai")
        from ..config import CUSTOM_MODEL_ALIASES

        model_ids = sorted(set(CUSTOM_MODEL_ALIASES.values()) | {DEFAULT_MODEL})
        return {
            "models": [
                {
                    "name": model_id,
                    "model": model_id,
                    "modified_at": "1970-01-01T00:00:00Z",
                    "size": 0,
                    "digest": "",
                    "details": {"family": "flowith", "parameter_size": "", "quantization_level": ""},
                }
                for model_id in model_ids
            ]
        }

    @router.get("/version")
    @router.get("/api/version")
    def version() -> dict[str, Any]:
        require_profile("codex", "openai")
        return {"version": __version__}

    @router.get("/props")
    @router.get("/v1/props")
    @router.get("/api/props")
    def props() -> dict[str, Any]:
        require_profile("codex", "openai")
        return {
            "service": "flowith-claude-proxy",
            "version": __version__,
            "capabilities": {
                "chat_completions": True,
                "responses": True,
                "models": True,
                "streaming": True,
            },
        }

    @router.post("/chat/completions")
    @router.post("/api/chat/completions")
    @router.post("/v1/chat/completions")
    async def create_chat_completion(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
        authorization: str | None = Header(default=None),
    ) -> Any:
        require_profile("codex", "openai")
        body = await read_json_object(request)
        api_key = require_api_key(x_api_key, authorization)

        requested_model = body.get("model") or DEFAULT_MODEL
        _trace_hermes(
            "route=chat_completions "
            f"path={request.url.path} "
            f"stream={bool(body.get('stream'))} "
            f"tools={len(body.get('tools') or [])} "
            f"tool_choice={body.get('tool_choice')!r} "
            f"model={requested_model!r}"
        )
        flowith_model = map_model(requested_model, default=DEFAULT_MODEL)
        client, upstream_model = get_client_for_key(api_key, flowith_model)
        messages = _chat_messages_from_body(body)
        chat_tools = _openai_tools_to_anthropic(body.get("tools") or [])
        if chat_tools:
            messages = _inject_responses_tool_prompt(
                messages,
                chat_tools,
                tool_choice=body.get("tool_choice"),
            )
        if not messages:
            raise HTTPException(status_code=400, detail="At least one message is required")

        stop_sequences = body.get("stop")
        if chat_tools:
            stop_sequences = with_xml_tool_stop_sequence(stop_sequences)

        if bool(body.get("stream")):
            return StreamingResponse(
                _chat_stream_events(
                    client,
                    messages,
                    requested_model,
                    upstream_model,
                    max_tokens=body.get("max_tokens"),
                    temperature=body.get("temperature"),
                    top_p=body.get("top_p"),
                    tools=chat_tools,
                    tool_choice=body.get("tool_choice"),
                    stop_sequences=stop_sequences,
                ),
                media_type="text/event-stream; charset=utf-8",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        result = await run_in_threadpool(
            client.call_api,
            messages,
            model=upstream_model,
            max_retries=2,
            stream=False,
            tools=None if chat_tools else body.get("tools"),
            tool_choice=None if chat_tools else body.get("tool_choice"),
            max_tokens=body.get("max_tokens"),
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            stop_sequences=stop_sequences,
        )
        if not result.get("success"):
            return _upstream_error_response(result)
        return JSONResponse(content=_chat_completion_response(result, requested_model))

    @router.post("/v1/responses")
    async def create_response(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
        authorization: str | None = Header(default=None),
    ) -> Any:
        require_profile("codex", "openai")
        body = await read_json_object(request)
        api_key = require_api_key(x_api_key, authorization)

        requested_model = body.get("model") or DEFAULT_MODEL
        _trace_hermes(
            "route=responses "
            f"path={request.url.path} "
            f"stream={bool(body.get('stream'))} "
            f"tools={len(body.get('tools') or [])} "
            f"tool_choice={body.get('tool_choice')!r} "
            f"model={requested_model!r}"
        )
        flowith_model = map_model(requested_model, default=DEFAULT_MODEL)
        client, upstream_model = get_client_for_key(api_key, flowith_model)
        messages = _responses_messages_from_input(body.get("input", ""))
        response_tools = _openai_tools_to_anthropic(body.get("tools") or [])
        if response_tools:
            messages = _inject_responses_tool_prompt(
                messages,
                response_tools,
                tool_choice=body.get("tool_choice"),
            )
        if not messages:
            raise HTTPException(status_code=400, detail="At least one input message is required")

        max_tokens = body.get("max_output_tokens", body.get("max_tokens"))
        stop_sequences = body.get("stop")
        if response_tools:
            stop_sequences = with_xml_tool_stop_sequence(stop_sequences)
        stream = bool(body.get("stream"))
        if stream:
            return StreamingResponse(
                _responses_stream_events(
                    client,
                    messages,
                    requested_model,
                    upstream_model,
                    max_tokens=max_tokens,
                    temperature=body.get("temperature"),
                    top_p=body.get("top_p"),
                    tools=response_tools,
                    stop_sequences=stop_sequences,
                ),
                media_type="text/event-stream; charset=utf-8",
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        result = await run_in_threadpool(
            client.call_api,
            messages,
            model=upstream_model,
            max_retries=2,
            stream=False,
            max_tokens=max_tokens,
            temperature=body.get("temperature"),
            top_p=body.get("top_p"),
            stop_sequences=stop_sequences,
        )
        if not result.get("success"):
            return _upstream_error_response(result)
        return JSONResponse(content=_responses_response(result, requested_model))

    return router


def _openai_sse(event: str | None, data: dict[str, Any] | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {payload}\n\n"
    return f"data: {payload}\n\n"


def _extract_openai_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type in {"text", "input_text", "output_text"}:
                parts.append(item.get("text", "") or "")
            elif "text" in item:
                parts.append(str(item.get("text") or ""))
            elif item_type in {"image", "image_url", "input_image"}:
                parts.append("[image omitted]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _chat_messages_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for raw_msg in body.get("messages", []) or []:
        if not isinstance(raw_msg, dict):
            continue
        role = raw_msg.get("role", "user")
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"

        if role == "assistant" and isinstance(raw_msg.get("tool_calls"), list):
            tool_call_parts: list[str] = []
            for tool_call in raw_msg.get("tool_calls") or []:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
                name = function.get("name") or tool_call.get("name", "")
                arguments = _json_arguments(function.get("arguments", tool_call.get("arguments", "{}")))
                tool_call_parts.append(format_tool_call_xml(name, arguments))
            content = _extract_openai_text(raw_msg.get("content"))
            if tool_call_parts:
                if content:
                    tool_call_parts.insert(0, content)
                messages.append({"role": "assistant", "content": "\n".join(tool_call_parts)})
                continue

        if role == "tool":
            messages.append({"role": "user", "content": format_observation_xml(_extract_openai_text(raw_msg.get("content")))})
            continue

        messages.append({"role": role, "content": _extract_openai_text(raw_msg.get("content"))})
    return messages


def _json_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {}


def _openai_tools_to_anthropic(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        return []

    converted: list[dict[str, Any]] = []
    for raw_tool in tools:
        if not isinstance(raw_tool, dict):
            continue

        tool_type = raw_tool.get("type")
        if tool_type == "function":
            func = raw_tool.get("function") if isinstance(raw_tool.get("function"), dict) else raw_tool
            name = func.get("name") or raw_tool.get("name")
            if not name:
                continue
            converted.append({
                "name": name,
                "description": func.get("description", raw_tool.get("description", "")) or "",
                "input_schema": func.get("parameters", raw_tool.get("parameters", {})) or {},
            })
            continue

        name = raw_tool.get("name") or tool_type
        if not name:
            continue
        schema = raw_tool.get("parameters") or raw_tool.get("input_schema") or {}
        if tool_type in {"shell", "local_shell", "shell_call", "local_shell_call"} and not schema:
            schema = {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in milliseconds"},
                },
                "required": ["command"],
            }
        converted.append({
            "name": name,
            "description": raw_tool.get("description", "") or f"Use the {name} tool.",
            "input_schema": schema,
        })

    return converted


def _openai_tool_choice_to_anthropic(tool_choice: Any) -> Any:
    if tool_choice in (None, "auto"):
        return "auto"
    if tool_choice == "none":
        return "none"
    if tool_choice == "required":
        return "any"
    if not isinstance(tool_choice, dict):
        return "auto"

    choice_type = tool_choice.get("type")
    if choice_type == "function":
        name = tool_choice.get("name")
        if not name and isinstance(tool_choice.get("function"), dict):
            name = tool_choice["function"].get("name")
        return {"type": "tool", "name": name} if name else "auto"
    if choice_type == "allowed_tools":
        mode = tool_choice.get("mode")
        if mode == "required":
            return "any"
        if mode == "none":
            return "none"
    return "auto"


def _inject_responses_tool_prompt(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: Any = None,
) -> list[dict[str, Any]]:
    xml_prompt = build_tool_xml_prompt(
        tools,
        tool_choice=_openai_tool_choice_to_anthropic(tool_choice),
    )
    if not xml_prompt:
        return messages

    injected = list(messages)
    if injected and injected[0].get("role") == "system":
        original = injected[0].get("content", "") or ""
        injected[0] = {
            "role": "system",
            "content": f"{xml_prompt}\n\n---\n\n{original}" if original else xml_prompt,
        }
    else:
        injected.insert(0, {"role": "system", "content": xml_prompt})
    return injected


def _responses_messages_from_input(raw_input: Any) -> list[dict[str, Any]]:
    if isinstance(raw_input, str):
        return [{"role": "user", "content": raw_input}]

    if not isinstance(raw_input, list):
        return [{"role": "user", "content": _extract_openai_text(raw_input)}]

    messages: list[dict[str, Any]] = []
    for item in raw_input:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue

        item_type = item.get("type")
        role = item.get("role", "user")
        if item_type == "function_call":
            messages.append({
                "role": "assistant",
                "content": format_tool_call_xml(
                    item.get("name", ""),
                    _json_arguments(item.get("arguments", "{}")),
                ),
            })
        elif item_type == "function_call_output":
            messages.append({
                "role": "user",
                "content": format_observation_xml(str(item.get("output", "") or "")),
            })
        elif item_type == "message" or "role" in item:
            if role not in {"system", "user", "assistant", "tool"}:
                role = "user"
            messages.append({"role": role, "content": _extract_openai_text(item.get("content"))})
        elif item_type in {"input_text", "output_text"}:
            messages.append({"role": "user", "content": item.get("text", "") or ""})

    return messages


def _finish_reason_openai(finish_reason: str | None) -> str:
    if finish_reason in {"length", "tool_calls", "content_filter"}:
        return finish_reason
    return "stop"


def _usage_openai(usage: dict[str, Any]) -> dict[str, int]:
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _usage_responses(usage: dict[str, Any]) -> dict[str, int]:
    prompt = int(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
    completion = int(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _chat_tool_call_item(tool: dict[str, Any]) -> dict[str, Any]:
    _, call_id = _responses_call_ids(tool.get("id"))
    arguments = json.dumps(
        tool.get("input", {}) if isinstance(tool.get("input"), dict) else {},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": tool.get("name", ""),
            "arguments": arguments,
        },
    }


def _chat_tool_calls_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    content = result.get("content", "") or ""
    items = [_chat_tool_call_item(tool) for tool in parse_xml_tool_calls(content)]

    for raw_tool in result.get("tool_calls") or []:
        if not isinstance(raw_tool, dict):
            continue
        func = raw_tool.get("function", {}) if isinstance(raw_tool.get("function"), dict) else {}
        items.append(_chat_tool_call_item({
            "id": raw_tool.get("id"),
            "name": func.get("name", ""),
            "input": _json_arguments(func.get("arguments", "{}")),
        }))

    return items


def _chat_completion_response(result: dict[str, Any], requested_model: str) -> dict[str, Any]:
    now = int(time.time())
    content = result.get("content", "") or ""
    reasoning = result.get("reasoning_content", "") or ""
    tool_calls = _chat_tool_calls_from_result(result)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = _finish_reason_openai(result.get("finish_reason"))
    if tool_calls:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
    if reasoning:
        # OpenAI-compatible reasoning field (DeepSeek/Qwen/most reasoning clients).
        message["reasoning_content"] = reasoning
    return {
        "id": "chatcmpl_" + new_message_id()[4:],
        "object": "chat.completion",
        "created": now,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": _usage_openai(result.get("usage", {}) or {}),
    }


_XML_STREAM_OPEN_MARKERS = ("<tool_call", "<function_calls>")


def _safe_non_xml_prefix(buffer: str, *, final: bool = False) -> tuple[str, str]:
    if not buffer:
        return "", ""
    if final:
        return buffer, ""

    keep = 0
    max_prefix_len = max(len(marker) for marker in _XML_STREAM_OPEN_MARKERS) - 1
    for size in range(1, min(len(buffer), max_prefix_len) + 1):
        suffix = buffer[-size:]
        if any(marker.startswith(suffix) for marker in _XML_STREAM_OPEN_MARKERS):
            keep = size

    if keep:
        return buffer[:-keep], buffer[-keep:]
    return buffer, ""


def _drain_xml_tool_stream_buffer(
    buffer: str,
    *,
    final: bool = False,
) -> tuple[list[tuple[str, Any]], str]:
    segments: list[tuple[str, Any]] = []

    while buffer:
        xml_start = find_xml_tool_call_start(buffer)
        if xml_start == -1:
            text, buffer = _safe_non_xml_prefix(buffer, final=final)
            if text:
                segments.append(("text", text))
            break

        if xml_start > 0:
            segments.append(("text", buffer[:xml_start]))
            buffer = buffer[xml_start:]

        xml_end = find_xml_tool_call_end(buffer)
        if xml_end == -1 and not final:
            break

        xml_source = buffer if xml_end == -1 else buffer[:xml_end]
        tools = parse_xml_tool_calls(xml_source)
        consumed_end = find_xml_tool_call_consumed_end(xml_source)
        if tools and consumed_end != -1:
            segments.append(("tools", tools))
            buffer = buffer[consumed_end:]
            continue

        if final:
            # A malformed tool marker should not leak back as assistant text.
            buffer = ""
            break
        break

    return segments, buffer


def _chat_stream_events(
    client: FlowithClient,
    messages: list[dict[str, Any]],
    requested_model: str,
    upstream_model: str | None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
    stop_sequences: list[str] | str | None = None,
) -> Generator[bytes, None, None]:
    completion_id = "chatcmpl_" + new_message_id()[4:]
    now = int(time.time())
    q: "queue.Queue[Any]" = queue.Queue(maxsize=512)
    result_holder: dict[str, Any] = {}

    has_tools = bool(tools)
    raw_stream_chunks = 0
    streamed_text_chunks = 0
    streamed_reasoning_chunks = 0
    xml_buffer = ""
    emitted_tool_calls: list[dict[str, Any]] = []

    def on_chunk(piece: str) -> None:
        if piece:
            q.put(("text", piece))

    def on_reasoning(piece: str) -> None:
        if piece:
            q.put(("reasoning", piece))

    def worker() -> None:
        try:
            result_holder["result"] = client.call_api(
                messages,
                model=upstream_model,
                max_retries=1,
                stream=True,
                on_chunk=on_chunk,
                on_reasoning=on_reasoning,
                tools=None if has_tools else tools,
                tool_choice=None if has_tools else tool_choice,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                stop_sequences=stop_sequences,
            )
        except Exception as e:
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            q.put(_SENTINEL_DONE)

    threading.Thread(target=worker, daemon=True).start()

    first_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": requested_model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    }
    yield _openai_sse(None, first_chunk).encode("utf-8")

    def _emit_text_delta(text: str) -> Generator[bytes, None, None]:
        nonlocal streamed_text_chunks
        if not text:
            return
        streamed_text_chunks += 1
        yield _openai_sse(
            None,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": requested_model,
                "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
            },
        ).encode("utf-8")

    def _emit_reasoning_delta(text: str) -> Generator[bytes, None, None]:
        nonlocal streamed_reasoning_chunks
        if not text:
            return
        streamed_reasoning_chunks += 1
        # OpenAI-compatible reasoning field (DeepSeek/Qwen/most reasoning-capable clients).
        yield _openai_sse(
            None,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"reasoning_content": text},
                        "finish_reason": None,
                    }
                ],
            },
        ).encode("utf-8")

    def _emit_tool_call_delta(tool_call: dict[str, Any]) -> Generator[bytes, None, None]:
        index = len(emitted_tool_calls)
        emitted_tool_calls.append(tool_call)
        tool_id = tool_call.get("id", f"call_{index}")
        func = tool_call.get("function", {}) if isinstance(tool_call.get("function"), dict) else {}
        yield _openai_sse(
            None,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": tool_id,
                                    "type": "function",
                                    "function": {"name": func.get("name", ""), "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
        ).encode("utf-8")
        yield _openai_sse(
            None,
            {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "function": {"arguments": func.get("arguments", "{}")},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
        ).encode("utf-8")

    def _emit_tool_segments(segments: list[tuple[str, Any]]) -> Generator[bytes, None, None]:
        for kind, payload in segments:
            if kind == "text":
                yield from _emit_text_delta(str(payload))
                continue
            if kind == "tools":
                for raw_tool in payload:
                    yield from _emit_tool_call_delta(_chat_tool_call_item(raw_tool))

    while True:
        item = q.get()
        if item is _SENTINEL_DONE:
            break
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        kind, payload = item
        if kind == "reasoning":
            yield from _emit_reasoning_delta(str(payload))
            continue
        if kind != "text":
            continue
        raw_stream_chunks += 1
        if has_tools:
            xml_buffer += str(payload)
            segments, xml_buffer = _drain_xml_tool_stream_buffer(xml_buffer)
            yield from _emit_tool_segments(segments)
            continue
        yield from _emit_text_delta(str(payload))

    result = result_holder.get("result") or {}
    _trace_hermes(
        "chat_stream_summary "
        f"has_tools={has_tools} "
        f"raw_stream_chunks={raw_stream_chunks} "
        f"streamed_text_chunks={streamed_text_chunks} "
        f"emitted_tool_calls={len(emitted_tool_calls)} "
        f"result_success={result.get('success')}"
    )
    if not result.get("success"):
        yield _openai_sse(
            None,
            {"error": {"type": "upstream_error", "message": str(result.get("error", "upstream error"))}},
        ).encode("utf-8")
        yield _openai_sse(None, "[DONE]").encode("utf-8")
        return

    if has_tools:
        if raw_stream_chunks == 0 and result.get("content"):
            xml_buffer += str(result.get("content") or "")
        segments, xml_buffer = _drain_xml_tool_stream_buffer(xml_buffer, final=True)
        yield from _emit_tool_segments(segments)
        native_tool_calls = _chat_tool_calls_from_result({
            "content": "",
            "tool_calls": result.get("tool_calls") or [],
        })
        for tool_call in native_tool_calls:
            yield from _emit_tool_call_delta(tool_call)

    _trace_hermes(
        "chat_stream_final "
        f"tool_calls={len(emitted_tool_calls)} "
        f"result_finish_reason={result.get('finish_reason')!r} "
        f"final_reason={('tool_calls' if has_tools and emitted_tool_calls else _finish_reason_openai(result.get('finish_reason')))!r}"
    )
    final_reason = "tool_calls" if has_tools and emitted_tool_calls else _finish_reason_openai(result.get("finish_reason"))
    final_chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": now,
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": final_reason,
            }
        ],
        "usage": _usage_openai(result.get("usage", {}) or {}),
    }
    yield _openai_sse(None, final_chunk).encode("utf-8")
    yield _openai_sse(None, "[DONE]").encode("utf-8")


def _responses_call_ids(raw_id: Any = "") -> tuple[str, str]:
    raw = str(raw_id or "")
    if raw.startswith("toolu_"):
        suffix = raw[6:]
    elif raw.startswith("call_"):
        suffix = raw[5:]
    elif raw.startswith("fc_"):
        suffix = raw[3:]
    else:
        suffix = raw or new_message_id()[4:]
    return f"fc_{suffix}", f"call_{suffix}"


def _responses_function_call_item(tool: dict[str, Any]) -> dict[str, Any]:
    item_id, call_id = _responses_call_ids(tool.get("id"))
    arguments = json.dumps(
        tool.get("input", {}) if isinstance(tool.get("input"), dict) else {},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "name": tool.get("name", ""),
        "arguments": arguments,
    }


def _responses_function_calls_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    content = result.get("content", "") or ""
    xml_tools = parse_xml_tool_calls(content)
    items = [_responses_function_call_item(tool) for tool in xml_tools]

    native_count = 0
    for raw_tool in result.get("tool_calls") or []:
        if not isinstance(raw_tool, dict):
            continue
        func = raw_tool.get("function", {}) if isinstance(raw_tool.get("function"), dict) else {}
        items.append(_responses_function_call_item({
            "id": raw_tool.get("id"),
            "name": func.get("name", ""),
            "input": _json_arguments(func.get("arguments", "{}")),
        }))
        native_count += 1

    _trace_hermes(
        "responses_function_calls "
        f"xml_count={len(xml_tools)} "
        f"native_count={native_count} "
        f"total={len(items)} "
        f"content_has_xml={('<tool_call>' in content)}"
    )

    return items


def _responses_response(result: dict[str, Any], requested_model: str) -> dict[str, Any]:
    now = int(time.time())
    content = result.get("content", "") or ""
    reasoning = result.get("reasoning_content", "") or ""
    response_id = "resp_" + new_message_id()[4:]
    message_id = "msg_" + new_message_id()[4:]
    function_calls = _responses_function_calls_from_result(result)
    output: list[dict[str, Any]] = []
    output_text = content
    if reasoning:
        output.append(
            {
                "id": "rs_" + new_message_id()[4:],
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": reasoning}
                ],
            }
        )
    if function_calls:
        output.extend(function_calls)
        output_text = ""
    else:
        output.append(
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content,
                        "annotations": [],
                    }
                ],
            }
        )
    return {
        "id": response_id,
        "object": "response",
        "created_at": now,
        "status": "completed",
        "model": requested_model,
        "output": output,
        "output_text": output_text,
        "usage": _usage_responses(result.get("usage", {}) or {}),
    }


def _upstream_error_response(result: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "type": "upstream_error",
                "message": str(result.get("error", "unknown upstream error")),
            }
        },
    )


def _responses_stream_events(
    client: FlowithClient,
    messages: list[dict[str, Any]],
    requested_model: str,
    upstream_model: str | None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    stop_sequences: list[str] | str | None = None,
) -> Generator[bytes, None, None]:
    response_id = "resp_" + new_message_id()[4:]
    message_id = "msg_" + new_message_id()[4:]
    now = int(time.time())
    q: "queue.Queue[Any]" = queue.Queue(maxsize=512)
    result_holder: dict[str, Any] = {}

    def on_chunk(piece: str) -> None:
        if piece:
            q.put(("text", piece))

    def on_reasoning(piece: str) -> None:
        if piece:
            q.put(("reasoning", piece))

    def worker() -> None:
        try:
            result_holder["result"] = client.call_api(
                messages,
                model=upstream_model,
                max_retries=1,
                stream=True,
                on_chunk=on_chunk,
                on_reasoning=on_reasoning,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                tools=None,
                tool_choice=None,
                stop_sequences=stop_sequences,
            )
        except Exception as e:
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            q.put(_SENTINEL_DONE)

    threading.Thread(target=worker, daemon=True).start()

    created = {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": now,
            "status": "in_progress",
            "model": requested_model,
            "output": [],
        },
    }
    yield _openai_sse("response.created", created).encode("utf-8")

    has_tools = bool(tools)
    text_item_started = False
    trace_id = response_id[-6:]
    _trace_hermes(f"responses_stream_start trace={trace_id} has_tools={has_tools}")

    def _emit_text_start() -> Generator[bytes, None, None]:
        nonlocal next_output_index, text_item_started, text_output_index
        if text_item_started:
            return
        text_item_started = True
        text_output_index = next_output_index
        next_output_index += 1
        _trace_hermes(f"responses_stream_event trace={trace_id} event=response.output_item.added kind=text")
        yield _openai_sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": text_output_index,
                "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": message_id,
                "output_index": text_output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ).encode("utf-8")

    output_text = ""
    xml_buffer = ""
    text_done_emitted = False
    emitted_function_calls: list[dict[str, Any]] = []
    completed_items: dict[int, dict[str, Any]] = {}
    next_output_index = 0
    text_output_index: int | None = None
    raw_stream_chunks = 0
    streamed_text_chunks = 0

    def _emit_text_delta(text: str) -> Generator[bytes, None, None]:
        nonlocal output_text, streamed_text_chunks
        if not text:
            return
        yield from _emit_text_start()
        output_text += text
        streamed_text_chunks += 1
        yield _openai_sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": message_id,
                "output_index": text_output_index if text_output_index is not None else 0,
                "content_index": 0,
                "delta": text,
            },
        ).encode("utf-8")

    def _emit_text_done(*, final_text: str | None = None) -> Generator[bytes, None, None]:
        nonlocal text_done_emitted
        if text_done_emitted or not text_item_started:
            return
        text_done_emitted = True
        reported_text = output_text if final_text is None else final_text
        output_index = text_output_index if text_output_index is not None else 0
        _trace_hermes(f"responses_stream_event trace={trace_id} event=response.output_text.done text_len={len(reported_text)}")
        yield _openai_sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "text": reported_text,
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": reported_text, "annotations": []},
            },
        ).encode("utf-8")
        message_item = {
            "id": message_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": reported_text, "annotations": []}],
        }
        completed_items[output_index] = message_item
        yield _openai_sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": message_item,
            },
        ).encode("utf-8")

    def _emit_function_call(function_call: dict[str, Any]) -> Generator[bytes, None, None]:
        nonlocal next_output_index
        output_index = next_output_index
        next_output_index += 1
        emitted_function_calls.append(function_call)
        completed_items[output_index] = function_call
        added_item = dict(function_call)
        added_item["status"] = "in_progress"
        added_item["arguments"] = ""
        _trace_hermes(
            f"responses_stream_event trace={trace_id} event=response.output_item.added kind=function_call output_index={output_index}"
        )
        yield _openai_sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": added_item,
            },
        ).encode("utf-8")
        arguments = function_call.get("arguments", "") or "{}"
        yield _openai_sse(
            "response.function_call_arguments.delta",
            {
                "type": "response.function_call_arguments.delta",
                "item_id": function_call["id"],
                "output_index": output_index,
                "delta": arguments,
            },
        ).encode("utf-8")
        _trace_hermes(
            f"responses_stream_event trace={trace_id} event=response.function_call_arguments.done output_index={output_index} args_len={len(arguments)}"
        )
        yield _openai_sse(
            "response.function_call_arguments.done",
            {
                "type": "response.function_call_arguments.done",
                "item_id": function_call["id"],
                "output_index": output_index,
                "arguments": arguments,
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": function_call,
            },
        ).encode("utf-8")

    def _emit_response_segments(segments: list[tuple[str, Any]]) -> Generator[bytes, None, None]:
        for kind, payload in segments:
            if kind == "text":
                yield from _emit_text_delta(str(payload))
                continue
            if kind == "tools":
                function_calls = [_responses_function_call_item(tool) for tool in payload]
                _trace_hermes(
                    f"responses_stream_tool_mode trace={trace_id} function_calls={len(function_calls)}"
                )
                for function_call in function_calls:
                    yield from _emit_function_call(function_call)

    reasoning_item_id = "rs_" + new_message_id()[4:]
    reasoning_item_started = False
    reasoning_item_done = False
    reasoning_output_index: int | None = None
    reasoning_text_accum = ""
    streamed_reasoning_chunks = 0

    def _emit_reasoning_start() -> Generator[bytes, None, None]:
        nonlocal next_output_index, reasoning_item_started, reasoning_output_index
        if reasoning_item_started:
            return
        reasoning_item_started = True
        reasoning_output_index = next_output_index
        next_output_index += 1
        _trace_hermes(
            f"responses_stream_event trace={trace_id} event=response.output_item.added kind=reasoning output_index={reasoning_output_index}"
        )
        yield _openai_sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": reasoning_output_index,
                "item": {
                    "id": reasoning_item_id,
                    "type": "reasoning",
                    "summary": [],
                },
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.reasoning_summary_part.added",
            {
                "type": "response.reasoning_summary_part.added",
                "item_id": reasoning_item_id,
                "output_index": reasoning_output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            },
        ).encode("utf-8")

    def _emit_reasoning_delta(text: str) -> Generator[bytes, None, None]:
        nonlocal reasoning_text_accum, streamed_reasoning_chunks
        if not text:
            return
        yield from _emit_reasoning_start()
        reasoning_text_accum += text
        streamed_reasoning_chunks += 1
        yield _openai_sse(
            "response.reasoning_summary_text.delta",
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": reasoning_item_id,
                "output_index": reasoning_output_index if reasoning_output_index is not None else 0,
                "summary_index": 0,
                "delta": text,
            },
        ).encode("utf-8")

    def _emit_reasoning_done(*, final_text: str | None = None) -> Generator[bytes, None, None]:
        nonlocal reasoning_item_done
        if reasoning_item_done or not reasoning_item_started:
            return
        reasoning_item_done = True
        reported = reasoning_text_accum if final_text is None else final_text
        output_index = reasoning_output_index if reasoning_output_index is not None else 0
        _trace_hermes(
            f"responses_stream_event trace={trace_id} event=response.reasoning_summary_text.done text_len={len(reported)}"
        )
        yield _openai_sse(
            "response.reasoning_summary_text.done",
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": reasoning_item_id,
                "output_index": output_index,
                "summary_index": 0,
                "text": reported,
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.reasoning_summary_part.done",
            {
                "type": "response.reasoning_summary_part.done",
                "item_id": reasoning_item_id,
                "output_index": output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": reported},
            },
        ).encode("utf-8")
        reasoning_item = {
            "id": reasoning_item_id,
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": reported}],
        }
        completed_items[output_index] = reasoning_item
        yield _openai_sse(
            "response.output_item.done",
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": reasoning_item,
            },
        ).encode("utf-8")

    while True:
        item = q.get()
        if item is _SENTINEL_DONE:
            break
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        kind, payload = item
        if kind == "reasoning":
            yield from _emit_reasoning_delta(str(payload))
            continue
        if kind != "text":
            continue
        raw_stream_chunks += 1
        if has_tools:
            xml_buffer += str(payload)
            segments, xml_buffer = _drain_xml_tool_stream_buffer(xml_buffer)
            yield from _emit_response_segments(segments)
            continue
        yield from _emit_text_delta(str(payload))

    if has_tools:
        if raw_stream_chunks == 0 and result_holder.get("result", {}).get("content"):
            xml_buffer += str(result_holder.get("result", {}).get("content") or "")
        segments, xml_buffer = _drain_xml_tool_stream_buffer(xml_buffer, final=True)
        yield from _emit_response_segments(segments)

    result = result_holder.get("result") or {}
    _trace_hermes(
        f"responses_stream_summary trace={trace_id} "
        f"raw_stream_chunks={raw_stream_chunks} "
        f"streamed_text_chunks={streamed_text_chunks} "
        f"output_text_len={len(output_text)} "
        f"emitted_function_calls={len(emitted_function_calls)} "
        f"result_success={result.get('success')}"
    )
    if not result.get("success"):
        yield _openai_sse(
            "error",
            {
                "type": "error",
                "error": {"type": "upstream_error", "message": str(result.get("error", "upstream error"))},
            },
        ).encode("utf-8")
        return

    usage = _usage_responses(result.get("usage", {}) or {})
    native_function_calls = _responses_function_calls_from_result({
        "content": "",
        "tool_calls": result.get("tool_calls") or [],
    })

    if has_tools and native_function_calls:
        _trace_hermes(
            f"responses_stream_tool_mode trace={trace_id} function_calls={len(native_function_calls)}"
        )
        for function_call in native_function_calls:
            yield from _emit_function_call(function_call)

    # Finalize reasoning first; if upstream only returned reasoning at the end (non-stream fallback),
    # emit whatever accumulated in result.reasoning_content.
    final_reasoning = result.get("reasoning_content", "") or ""
    if final_reasoning and not reasoning_item_started:
        yield from _emit_reasoning_delta(final_reasoning)
    yield from _emit_reasoning_done(
        final_text=final_reasoning if final_reasoning else None
    )

    if not text_item_started and not emitted_function_calls:
        yield from _emit_text_start()

    yield from _emit_text_done(final_text="" if has_tools else None)

    completed_output = [completed_items[index] for index in sorted(completed_items)]

    completed_mode = "function_calls" if emitted_function_calls else "text"
    _trace_hermes(f"responses_stream_event trace={trace_id} event=response.completed mode={completed_mode}")
    yield _openai_sse(
        "response.completed",
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "object": "response",
                "created_at": now,
                "status": "completed",
                "model": requested_model,
                "output": completed_output,
                "output_text": "" if has_tools else output_text,
                "usage": usage,
            },
        },
    ).encode("utf-8")
    yield _openai_sse(None, "[DONE]").encode("utf-8")
