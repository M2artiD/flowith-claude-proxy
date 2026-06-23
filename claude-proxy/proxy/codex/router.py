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

from ..adapter import (
    build_tool_xml_prompt,
    format_observation_xml,
    format_tool_call_xml,
    map_model,
    new_message_id,
    parse_xml_tool_calls,
)
from .. import __version__
from ..config import DEFAULT_MODEL
from ..upstream import FlowithClient

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

        result = client.call_api(
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

        result = client.call_api(
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
    tool_calls = _chat_tool_calls_from_result(result)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = _finish_reason_openai(result.get("finish_reason"))
    if tool_calls:
        message = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        finish_reason = "tool_calls"
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
    buffered_tool_mode_text: list[str] = []

    def on_chunk(piece: str) -> None:
        if piece:
            q.put(("text", piece))

    def worker() -> None:
        try:
            result_holder["result"] = client.call_api(
                messages,
                model=upstream_model,
                max_retries=1,
                stream=True,
                on_chunk=on_chunk,
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

    while True:
        item = q.get()
        if item is _SENTINEL_DONE:
            break
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        kind, payload = item
        if kind != "text":
            continue
        if has_tools:
            buffered_tool_mode_text.append(str(payload))
            continue
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": requested_model,
            "choices": [{"index": 0, "delta": {"content": payload}, "finish_reason": None}],
        }
        yield _openai_sse(None, chunk).encode("utf-8")

    result = result_holder.get("result") or {}
    if not result.get("success"):
        yield _openai_sse(
            None,
            {"error": {"type": "upstream_error", "message": str(result.get("error", "upstream error"))}},
        ).encode("utf-8")
        yield _openai_sse(None, "[DONE]").encode("utf-8")
        return

    tool_calls = _chat_tool_calls_from_result(result)
    if has_tools and not tool_calls:
        content = result.get("content", "") or "".join(buffered_tool_mode_text)
        if content:
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": now,
                "model": requested_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": content},
                        "finish_reason": None,
                    }
                ],
            }
            yield _openai_sse(None, chunk).encode("utf-8")

    if has_tools and tool_calls:
        for index, tool_call in enumerate(tool_calls):
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

    final_reason = "tool_calls" if has_tools and tool_calls else _finish_reason_openai(result.get("finish_reason"))
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
    items = [_responses_function_call_item(tool) for tool in parse_xml_tool_calls(content)]

    for raw_tool in result.get("tool_calls") or []:
        if not isinstance(raw_tool, dict):
            continue
        func = raw_tool.get("function", {}) if isinstance(raw_tool.get("function"), dict) else {}
        items.append(_responses_function_call_item({
            "id": raw_tool.get("id"),
            "name": func.get("name", ""),
            "input": _json_arguments(func.get("arguments", "{}")),
        }))

    return items


def _responses_response(result: dict[str, Any], requested_model: str) -> dict[str, Any]:
    now = int(time.time())
    content = result.get("content", "") or ""
    response_id = "resp_" + new_message_id()[4:]
    message_id = "msg_" + new_message_id()[4:]
    function_calls = _responses_function_calls_from_result(result)
    output: list[dict[str, Any]]
    output_text = content
    if function_calls:
        output = function_calls
        output_text = ""
    else:
        output = [
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
        ]
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

    def worker() -> None:
        try:
            result_holder["result"] = client.call_api(
                messages,
                model=upstream_model,
                max_retries=1,
                stream=True,
                on_chunk=on_chunk,
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

    def _emit_text_start() -> Generator[bytes, None, None]:
        nonlocal text_item_started
        if text_item_started:
            return
        text_item_started = True
        yield _openai_sse(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": message_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ).encode("utf-8")

    if not has_tools:
        yield from _emit_text_start()

    output_text = ""
    while True:
        item = q.get()
        if item is _SENTINEL_DONE:
            break
        if not isinstance(item, tuple) or len(item) != 2:
            continue
        kind, payload = item
        if kind != "text":
            continue
        output_text += payload
        if has_tools:
            continue
        yield _openai_sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "item_id": message_id,
                "output_index": 0,
                "content_index": 0,
                "delta": payload,
            },
        ).encode("utf-8")

    result = result_holder.get("result") or {}
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
    function_calls = _responses_function_calls_from_result({
        "content": output_text,
        "tool_calls": result.get("tool_calls") or [],
    })

    if has_tools and function_calls:
        for output_index, function_call in enumerate(function_calls):
            added_item = dict(function_call)
            added_item["status"] = "in_progress"
            added_item["arguments"] = ""
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
                    "output": function_calls,
                    "output_text": "",
                    "usage": usage,
                },
            },
        ).encode("utf-8")
        yield _openai_sse(None, "[DONE]").encode("utf-8")
        return

    if has_tools and not text_item_started:
        yield from _emit_text_start()
        if output_text:
            yield _openai_sse(
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": message_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": output_text,
                },
            ).encode("utf-8")

    yield _openai_sse(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "text": output_text,
        },
    ).encode("utf-8")
    yield _openai_sse(
        "response.content_part.done",
        {
            "type": "response.content_part.done",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": output_text, "annotations": []},
        },
    ).encode("utf-8")
    yield _openai_sse(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": output_text, "annotations": []}],
            },
        },
    ).encode("utf-8")
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
                "output": [
                    {
                        "id": message_id,
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": output_text, "annotations": []}],
                    }
                ],
                "output_text": output_text,
                "usage": usage,
            },
        },
    ).encode("utf-8")
    yield _openai_sse(None, "[DONE]").encode("utf-8")
