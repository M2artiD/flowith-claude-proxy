"""Codex/OpenAI-compatible routes for the Flowith proxy."""

from __future__ import annotations

import json
import queue
import re
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
from ..config import (
    DEFAULT_MODEL,
    FLOWITH_MIN_MAX_TOKENS,
    FLOWITH_RESPONSES_COMPACT_FINAL_TEXT,
    FLOWITH_SSE_HEARTBEAT_INTERVAL,
    FLOWITH_TRACE_HERMES,
)
from ..upstream import FlowithClient


_TRACE_HERMES = FLOWITH_TRACE_HERMES
_SSE_HEARTBEAT_INTERVAL = max(1.0, FLOWITH_SSE_HEARTBEAT_INTERVAL)

_THINK_OPEN_TAG = "<think>"
_THINK_CLOSE_TAG = "</think>"


def _floor_max_tokens(value: Any) -> int:
    """Clamp a client-supplied max_tokens up to FLOWITH_MIN_MAX_TOKENS.

    gpt-5.6 spends budget on a long reasoning preamble before emitting the XML
    tool call; a small client cap (e.g. 256) truncates mid-parameters, leaving
    malformed tags the parser cannot recover. Flooring guarantees the tool call
    fits. None (unset) also gets the floor.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 0
    return max(n, FLOWITH_MIN_MAX_TOKENS)


def _split_stream_think_segments(
    raw: str,
    in_think: bool,
) -> tuple[list[tuple[str, str]], str, bool]:
    """Split an incoming stream fragment into ordered ('text'|'reasoning', str) segments.

    - Content inside <think>...</think> pairs is routed to 'reasoning' so the codex
      UI can render it as thinking output instead of losing it.
    - A trailing partial tag (e.g. ``<th``, ``</thin``) is kept in the pending buffer
      so the next chunk can complete it without leaking the marker as visible text.
    - When ``in_think`` is True the fragment starts inside a thinking block; the
      returned boolean tells the caller whether the block is still open after
      consuming ``raw``.
    """
    segments: list[tuple[str, str]] = []
    pos = 0

    def _append(kind: str, text: str) -> None:
        if not text:
            return
        if segments and segments[-1][0] == kind:
            segments[-1] = (kind, segments[-1][1] + text)
            return
        segments.append((kind, text))

    while pos < len(raw):
        if in_think:
            close_idx = raw.find(_THINK_CLOSE_TAG, pos)
            if close_idx >= 0:
                _append("reasoning", raw[pos:close_idx])
                pos = close_idx + len(_THINK_CLOSE_TAG)
                in_think = False
                continue

            remainder = raw[pos:]
            for i in range(min(len(_THINK_CLOSE_TAG) - 1, len(remainder)), 0, -1):
                if remainder.endswith(_THINK_CLOSE_TAG[:i]):
                    _append("reasoning", remainder[:-i])
                    return segments, remainder[-i:], True
            _append("reasoning", remainder)
            return segments, "", True

        open_idx = raw.find(_THINK_OPEN_TAG, pos)
        if open_idx >= 0:
            _append("text", raw[pos:open_idx])
            pos = open_idx + len(_THINK_OPEN_TAG)
            in_think = True
            continue

        remainder = raw[pos:]
        for i in range(min(len(_THINK_OPEN_TAG) - 1, len(remainder)), 0, -1):
            if remainder.endswith(_THINK_OPEN_TAG[:i]):
                _append("text", remainder[:-i])
                return segments, remainder[-i:], False
        _append("text", remainder)
        return segments, "", False

    return segments, "", in_think


def _extract_think_from_text(text: str) -> tuple[str, str]:
    """Strip <think>...</think> pairs from a non-streaming string.

    Returns (visible_text, extracted_reasoning). If an unclosed opening tag
    is present, the tail is still routed into reasoning so nothing is lost.
    """
    if not text or _THINK_OPEN_TAG not in text:
        return text, ""
    segments, tail, in_think = _split_stream_think_segments(text, False)
    if tail:
        segments.append(("reasoning" if in_think else "text", tail))
    visible = "".join(t for k, t in segments if k == "text")
    reasoning_out = "".join(t for k, t in segments if k == "reasoning")
    return visible, reasoning_out


def _trace_hermes(message: str) -> None:
    if _TRACE_HERMES:
        print(f"[HERMES-TRACE] {message}", flush=True)

_SENTINEL_DONE = object()

ReadJsonObject = Callable[[Request], Awaitable[dict[str, Any]]]
RequireProfile = Callable[..., None]
RequireApiKey = Callable[[str | None, str | None], str]
RequireDiscoveryApiKey = Callable[[str | None, str | None], None]
GetClientForKey = Callable[[str, str], tuple[FlowithClient, str | None]]
WithXmlToolStopSequence = Callable[[list[str] | str | None], list[str]]
RequestLogEnabled = Callable[[], bool]


def _request_log_message_count(body: dict[str, Any], route: str) -> int:
    if route == "chat_completions":
        return len(body.get("messages") or [])
    if route == "responses":
        raw_input = body.get("input", "")
        if isinstance(raw_input, list):
            return len(raw_input)
        return 1 if raw_input is not None else 0
    return 0


def _log_request_summary(
    *,
    route: str,
    path: str,
    body: dict[str, Any],
    requested_model: str,
) -> None:
    print(
        f"[REQ] route={route} "
        f"path={path} "
        f"model={requested_model} "
        f"tools={len(body.get('tools') or [])} "
        f"msgs={_request_log_message_count(body, route)} "
        f"max_tokens={body.get('max_output_tokens', body.get('max_tokens'))} "
        f"stream={bool(body.get('stream'))}",
        flush=True,
    )


def create_router(
    *,
    require_profile: RequireProfile,
    read_json_object: ReadJsonObject,
    require_api_key: RequireApiKey,
    require_discovery_api_key: RequireDiscoveryApiKey,
    get_client_for_key: GetClientForKey,
    with_xml_tool_stop_sequence: WithXmlToolStopSequence,
    request_log_enabled: RequestLogEnabled = lambda: False,
) -> APIRouter:
    """Build the Codex/OpenAI router while reusing server-owned auth/client state."""

    router = APIRouter()

    @router.get("/models")
    @router.get("/v1/models")
    def list_models(
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_profile("codex", "openai")
        require_discovery_api_key(x_api_key, authorization)
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
    def list_api_v1_models(
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return list_models(x_api_key, authorization)

    @router.get("/api/tags")
    def list_ollama_tags(
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_profile("codex", "openai")
        require_discovery_api_key(x_api_key, authorization)
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
    def version(
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_profile("codex", "openai")
        require_discovery_api_key(x_api_key, authorization)
        return {"version": __version__}

    @router.get("/props")
    @router.get("/v1/props")
    @router.get("/api/props")
    def props(
        x_api_key: str | None = Header(default=None, alias="x-api-key"),
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_profile("codex", "openai")
        require_discovery_api_key(x_api_key, authorization)
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
        if request_log_enabled():
            _log_request_summary(
                route="chat_completions",
                path=request.url.path,
                body=body,
                requested_model=requested_model,
            )
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
                    max_tokens=_floor_max_tokens(body.get("max_tokens")),
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
            max_tokens=_floor_max_tokens(body.get("max_tokens")),
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
        if request_log_enabled():
            _log_request_summary(
                route="responses",
                path=request.url.path,
                body=body,
                requested_model=requested_model,
            )
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
        response_tool_choice = _responses_tool_choice(
            body,
            requested_model,
            flowith_model,
        )
        tool_result_followup = (
            _responses_latest_is_tool_output(body)
            and _is_gpt_5_6_model(requested_model, flowith_model)
        )
        if response_tools:
            messages = _inject_responses_tool_prompt(
                messages,
                response_tools,
                tool_choice=response_tool_choice,
                tool_result_followup=tool_result_followup,
            )
        if not messages:
            raise HTTPException(status_code=400, detail="At least one input message is required")

        max_tokens = _floor_max_tokens(body.get("max_output_tokens", body.get("max_tokens")))
        enable_thinking, thinking_budget_tokens = _responses_thinking_options(body)
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
                    require_tool=_tool_choice_requires_call(response_tool_choice),
                    correct_no_tool_progress=tool_result_followup,
                    thinking=enable_thinking,
                    thinking_budget_tokens=thinking_budget_tokens,
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
            thinking=enable_thinking,
            thinking_budget_tokens=thinking_budget_tokens,
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
        from ..adapter import _loads_tool_params
        return _loads_tool_params(raw)
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


def _tool_choice_requires_call(tool_choice: Any) -> bool:
    normalized = _openai_tool_choice_to_anthropic(tool_choice)
    return normalized == "any" or (
        isinstance(normalized, dict) and normalized.get("type") == "tool"
    )


def _inject_responses_tool_prompt(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: Any = None,
    tool_result_followup: bool = False,
) -> list[dict[str, Any]]:
    normalized_choice = _openai_tool_choice_to_anthropic(tool_choice)
    xml_prompt = build_tool_xml_prompt(
        tools,
        tool_choice=normalized_choice,
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

    tool_call_required = _tool_choice_requires_call(tool_choice)
    if tool_call_required:
        injected.append({
            "role": "system",
            "content": (
                "TOOL CALL REQUIRED FOR THIS TURN. Do not answer with standalone prose, "
                "explain how the user could do the action, promise to do it later, or ask "
                "the user to do it. First output a concise user-visible action note: an "
                "execution summary stating the operational reason, tool name, and exact command or concrete "
                "action. For a simple action use one line. If the user explicitly asks for "
                "a plan, or the task involves design, debugging, repair, or verification, "
                "use a short public decision brief with at most four items covering: the "
                "current diagnosis/design choice, the key constraint or tradeoff, the exact "
                "next tool action, and the evidence that will validate it. This "
                "summary is not hidden chain-of-thought and must not include a "
                "result, success/failure claim, or final answer. Immediately then output "
                "the XML tool call and stop. Only after a real tool observation may you "
                "report success or failure."
            ),
        })
    elif tool_result_followup:
        injected.append({
            "role": "system",
            "content": (
                "TOOL RESULT FOLLOW-UP. Re-evaluate the user's full requested outcome "
                "against the latest observation. If any requested work remains, call the "
                "next available tool now. Do not end this turn with a progress update or "
                "future-tense promise such as 'I will', 'next I will', or 'I am going to'. "
                "Before that call, output one concise user-visible action note naming the "
                "tool and exact command or concrete action. For non-trivial remaining work, "
                "include a short public decision brief covering the current diagnosis, key "
                "tradeoff, next action, and validation evidence. The note must not include a "
                "result, success/failure claim, or final answer; then immediately emit the "
                "XML tool call. "
                "Answer in prose only when the observations prove the requested work is "
                "already complete; otherwise emit the XML tool call and stop."
            ),
        })
    return injected


_RESPONSES_REASONING_BUDGETS = {
    "minimal": 512,
    "low": 1024,
    "medium": 2048,
    "high": 4096,
    "xhigh": 6144,
}


def _responses_thinking_options(body: dict[str, Any]) -> tuple[bool | None, int | None]:
    reasoning = body.get("reasoning")
    if not isinstance(reasoning, dict):
        return None, None

    effort = str(reasoning.get("effort") or "").strip().lower()
    summary = str(reasoning.get("summary") or "").strip().lower()
    enabled = effort not in {"", "none"} or summary not in {"", "none"}
    if not enabled:
        return False, None

    budget = _RESPONSES_REASONING_BUDGETS.get(effort)
    if budget is None:
        budget = _RESPONSES_REASONING_BUDGETS["medium"]
    return True, budget


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
        if item_type in {"function_call", "custom_tool_call"}:
            raw_arguments = item.get("arguments", item.get("input", "{}"))
            messages.append({
                "role": "assistant",
                "content": format_tool_call_xml(
                    item.get("name", ""),
                    _json_arguments(raw_arguments),
                ),
            })
        elif item_type in {"function_call_output", "custom_tool_call_output"}:
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


def _responses_tool_choice(
    body: dict[str, Any],
    requested_model: str,
    effective_model: str | None = None,
) -> Any:
    choice = body.get("tool_choice")
    latest_is_tool_output = _responses_latest_is_tool_output(body)
    is_gpt_5_6 = _is_gpt_5_6_model(requested_model, effective_model)
    if (
        choice in (None, "auto")
        and is_gpt_5_6
        and not latest_is_tool_output
        and _responses_turn_explicitly_requests_action(body)
    ):
        return "required"
    return choice


_CN_ACTION_VERBS = (
    "打开|启动|运行|执行|调用|读取|查看|检查|核对|搜索|查询|联网|下载|上传|"
    "创建|新建|生成|编写|制作|做成|提炼|整理|归纳|转换|转成|添加|加入|写入|保存|修改|编辑|调整|修复|处理|删除|移动|复制|"
    "安装|构建|测试|验证|截图|点击|停止|终止|重启|部署|提交|推送|切换|完成"
)
_EN_ACTION_VERBS = (
    "open|launch|start|run|execute|call|read|inspect|check|search|browse|download|"
    "upload|create|generate|code|make|write|save|edit|modify|adjust|fix|handle|delete|move|"
    "copy|install|build|test|verify|click|screenshot|stop|restart|deploy|commit|push|switch"
)
_CN_DIRECT_WRITE_ACTION = r"写(?=\s*(?:一个|个|份|段|到|在|好|完|出))"
_CN_ACTION = rf"(?:{_CN_ACTION_VERBS}|{_CN_DIRECT_WRITE_ACTION})"
_NEGATED_ACTION_RE = re.compile(
    rf"(?:不要|别|无需|不用|不必)\s*(?:再\s*)?(?:帮我|为我|替我)?\s*(?:{_CN_ACTION})"
    rf"|\b(?:do\s+not|don't|dont|never)\s+(?:please\s+)?(?:{_EN_ACTION_VERBS})\b",
    re.IGNORECASE,
)
_EXPLICIT_ACTION_RE = re.compile(
    rf"(?:^|[。！？!?；;]\s*)"
    rf"(?:请(?:你)?|请务必|麻烦(?:你)?|帮我|为我|给我|替我|现在|马上|立即|快点|继续|直接|"
    rf"必须(?:实际)?|务必(?:实际)?|需要(?:你)?(?:实际)?)?\s*(?:{_CN_ACTION})"
    rf"|(?:^|[.!?;]\s*)"
    rf"(?:please\s+|can\s+you\s+|could\s+you\s+|i\s+need\s+you\s+to\s+|"
    rf"(?:you\s+)?must\s+(?:actually\s+)?)?(?:{_EN_ACTION_VERBS})\b"
    rf"|(?:^|[.!?;]\s*)use\b.{{0,80}}?\bto\s+(?:{_EN_ACTION_VERBS})\b"
    rf"|(?:帮我|为我|给我|替我)\s*(?:{_CN_ACTION})",
    re.IGNORECASE,
)
_TERSE_ACTION_CONTINUATION_RE = re.compile(
    r"^(?:(?:去吧|继续|开始|动手|照做|执行吧|做吧|快点|赶紧|接着做)(?:啊|呀|吧|呢)?|"
    r"go\s+ahead|do\s+it|proceed|continue|start\s+now)[。.!！]?\s*$",
    re.IGNORECASE,
)
_ACTION_FAILURE_REPORT_RE = re.compile(
    r"(?:未能|失败|报错|错误|打不开|无法加载|没(?:有)?载入|不能用|坏了|崩溃|"
    r"doesn['’]?t\s+work|didn['’]?t\s+work|failed|error|won['’]?t\s+load|not\s+loading)",
    re.IGNORECASE,
)
_EXPLANATION_QUESTION_RE = re.compile(
    r"(?:为什么|怎么回事|是什么意思|请解释|解释一下|\bwhy\b|what\s+does|how\s+come)",
    re.IGNORECASE,
)
_EXPLANATION_ONLY_RE = re.compile(
    r"(?:只(?:需|要)?\s*(?:解释|说明|回答)|仅\s*(?:解释|说明|回答)|"
    r"\bonly\s+(?:explain|describe|answer)\b)",
    re.IGNORECASE,
)


def _text_explicitly_requests_action(text: str) -> bool:
    if not text:
        return False
    without_negated_actions = _NEGATED_ACTION_RE.sub("", text)
    return bool(_EXPLICIT_ACTION_RE.search(without_negated_actions))


def _responses_turn_explicitly_requests_action(body: dict[str, Any]) -> bool:
    user_texts = _responses_user_texts(body)
    if not user_texts:
        return False
    latest = user_texts[-1].strip()
    if _EXPLANATION_QUESTION_RE.search(latest) and _EXPLANATION_ONLY_RE.search(latest):
        return False
    if _text_explicitly_requests_action(latest):
        return True
    if _EXPLANATION_QUESTION_RE.search(latest):
        return False
    continues_prior_action = bool(
        _TERSE_ACTION_CONTINUATION_RE.fullmatch(latest)
        or _ACTION_FAILURE_REPORT_RE.search(latest)
    )
    return continues_prior_action and any(
        _text_explicitly_requests_action(text) for text in user_texts[:-1]
    )


def _responses_user_texts(body: dict[str, Any]) -> list[str]:
    raw_input = body.get("input")
    if isinstance(raw_input, str):
        return [raw_input]
    if not isinstance(raw_input, list):
        return []

    texts: list[str] = []
    for item in raw_input:
        if isinstance(item, str):
            texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        role = item.get("role")
        if item_type == "message" and role in (None, "user"):
            texts.append(_extract_openai_text(item.get("content")))
        elif role == "user":
            texts.append(_extract_openai_text(item.get("content")))
        elif item_type == "input_text":
            texts.append(str(item.get("text", "") or ""))
    return [text for text in texts if text]


def _responses_latest_user_text(body: dict[str, Any]) -> str:
    user_texts = _responses_user_texts(body)
    return user_texts[-1] if user_texts else ""


def _responses_latest_is_tool_output(body: dict[str, Any]) -> bool:
    raw_input = body.get("input")
    if not isinstance(raw_input, list):
        return False

    last_user_index = -1
    last_tool_output_index = -1
    for index, item in enumerate(raw_input):
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        role = item.get("role")
        if item_type in {"function_call_output", "custom_tool_call_output"}:
            last_tool_output_index = index
        if role == "user" or (item_type == "message" and role in (None, "user")):
            last_user_index = index
    return last_tool_output_index > last_user_index


def _is_gpt_5_6_model(requested_model: str, effective_model: str | None = None) -> bool:
    return requested_model.startswith("gpt-5.6") or (
        isinstance(effective_model, str) and effective_model.startswith("gpt-5.6")
    )


def _finish_reason_openai(finish_reason: str | None) -> str:
    if finish_reason in {"length", "tool_calls", "content_filter"}:
        return finish_reason
    return "stop"


def _tool_call_truncated(result: dict[str, Any]) -> bool:
    # A partial XML tool call (opened tag, never closed) parses via
    # allow_partial and would otherwise ship as a successful tool_calls
    # result with malformed / half-populated arguments. Only treat it as a
    # cutoff when upstream actually hit its token ceiling -- a bare "<tool_call"
    # prefix under a normal stop is prose, not a truncated call.
    if _finish_reason_openai(result.get("finish_reason")) != "length":
        return False
    content = result.get("content", "") or ""
    if find_xml_tool_call_start(content) == -1:
        return False
    if find_xml_tool_call_end(content) == -1:
        return True
    # Closed the tag but nothing parsed cleanly -> arguments got cut off.
    return not parse_xml_tool_calls(content)


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
    content, extracted_reasoning = _extract_think_from_text(content)
    if extracted_reasoning:
        reasoning = (reasoning + "\n" if reasoning else "") + extracted_reasoning
    truncated = _tool_call_truncated(result)
    tool_calls = [] if truncated else _chat_tool_calls_from_result(result)
    if not content and not tool_calls and not truncated and reasoning:
        content = reasoning
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = _finish_reason_openai(result.get("finish_reason"))
    if truncated:
        finish_reason = "length"
    elif tool_calls:
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
        if xml_end == -1 and final and ">" not in buffer:
            # EOF while holding only a possible opening marker prefix.  Since no
            # full tag was ever confirmed, preserve it as visible assistant text
            # instead of treating it as malformed tool XML.
            segments.append(("text", buffer))
            buffer = ""
            break

        xml_source = buffer if xml_end == -1 else buffer[:xml_end]
        tools = parse_xml_tool_calls(xml_source)
        consumed_end = find_xml_tool_call_consumed_end(xml_source)
        if tools and consumed_end != -1:
            segments.append(("tools", tools))
            buffer = buffer[consumed_end:]
            continue

        if final:
            if any(marker.startswith(buffer) for marker in _XML_STREAM_OPEN_MARKERS):
                # This was only held to disambiguate a possible marker split at
                # chunk boundary.  No later chunk arrived, so it is visible text.
                segments.append(("text", buffer))
            # Other malformed/partial tool XML should not leak back as assistant text.
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
    cancel_event = threading.Event()

    has_tools = bool(tools)
    raw_stream_chunks = 0
    streamed_text_chunks = 0
    streamed_reasoning_chunks = 0
    xml_buffer = ""
    think_in = False
    think_buffer = ""
    emitted_tool_calls: list[dict[str, Any]] = []

    def _safe_put(item: Any) -> None:
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
                cancel_event=cancel_event,
            )
        except Exception as e:
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            _safe_put(_SENTINEL_DONE)

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

    try:
        while True:
            try:
                item = q.get(timeout=_SSE_HEARTBEAT_INTERVAL)
            except queue.Empty:
                # SSE comment heartbeat: keeps intermediaries alive and gives
                # the generator a chance to observe client disconnect.
                yield b": ping\n\n"
                continue
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
            think_buffer += str(payload)
            think_segments, think_buffer, think_in = _split_stream_think_segments(
                think_buffer, think_in
            )
            for seg_kind, seg_text in think_segments:
                if not seg_text:
                    continue
                if seg_kind == "reasoning":
                    yield from _emit_reasoning_delta(seg_text)
                    continue
                if has_tools:
                    xml_buffer += seg_text
                    segments, xml_buffer = _drain_xml_tool_stream_buffer(xml_buffer)
                    yield from _emit_tool_segments(segments)
                    continue
                yield from _emit_text_delta(seg_text)
    finally:
        cancel_event.set()

    if think_buffer and not think_in:
        if has_tools:
            xml_buffer += think_buffer
        else:
            yield from _emit_text_delta(think_buffer)
        think_buffer = ""

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
        # Flush whatever was held back in xml_buffer to disambiguate a possible
        # tool-call marker split across chunks -- on error there is no later
        # chunk coming, so treat it as plain text instead of dropping it.
        if xml_buffer:
            segments, xml_buffer = _drain_xml_tool_stream_buffer(xml_buffer, final=True)
            yield from _emit_tool_segments(segments)
        yield _openai_sse(
            None,
            {"error": {"type": "upstream_error", "message": str(result.get("error", "upstream error"))}},
        ).encode("utf-8")
        yield _openai_sse(None, "[DONE]").encode("utf-8")
        return

    # If the upstream never streamed reasoning chunks but returned reasoning_content
    # in the final coalesced result (non-stream fallback), emit it now so clients
    # don't see an empty reasoning field even though upstream produced real content.
    final_reasoning = result.get("reasoning_content", "") or ""
    if final_reasoning and streamed_reasoning_chunks == 0:
        yield from _emit_reasoning_delta(final_reasoning)

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
    else:
        # Same fallback as _responses_stream_events: if upstream returned no
        # incremental text chunks but has final content, flush it here instead
        # of silently emitting a blank assistant turn.
        final_content = result.get("content", "") or ""
        if final_content and streamed_text_chunks == 0:
            yield from _emit_text_delta(final_content)

    _trace_hermes(
        "chat_stream_final "
        f"tool_calls={len(emitted_tool_calls)} "
        f"result_finish_reason={result.get('finish_reason')!r} "
        f"final_reason={('tool_calls' if has_tools and emitted_tool_calls else _finish_reason_openai(result.get('finish_reason')))!r}"
    )
    if _tool_call_truncated(result):
        final_reason = "length"
    else:
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
    content, extracted_reasoning = _extract_think_from_text(content)
    if extracted_reasoning:
        reasoning = (reasoning + "\n" if reasoning else "") + extracted_reasoning
    response_id = "resp_" + new_message_id()[4:]
    message_id = "msg_" + new_message_id()[4:]
    truncated = _tool_call_truncated(result)
    function_calls = [] if truncated else _responses_function_calls_from_result(result)
    if not content and not function_calls and not truncated and reasoning:
        content = reasoning
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
    response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": now,
        "status": "incomplete" if truncated else "completed",
        "model": requested_model,
        "output": output,
        "output_text": output_text,
        "usage": _usage_responses(result.get("usage", {}) or {}),
    }
    if truncated:
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    return response


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


_PLAN_LINE_RE = re.compile(r"^(?:[-*\u2022]|\d{1,2}[.)]|[（(]\d{1,2}[）)])\s*")
_PLAN_HEADER_RE = re.compile(
    r"(?:plan|steps?|计划|步骤|执行方案|决策摘要|执行摘要|实施判断|"
    r"decision\s+brief|execution\s+brief)[：:]?\s*$",
    re.IGNORECASE,
)
_UNFINISHED_PROGRESS_RE = re.compile(
    r"(?:\b(?:i(?:'ll| will| am going to)|next\s+i(?:'ll| will))\b.{0,120}"
    r"(?:use|run|read|check|write|open|verify|create)|"
    r"(?:^我用|我(?:先|会|将|现在)|接下来|下一步).{0,120}"
    r"(?:使用|调用|执行|读取|检查|写入|打开|验证|创建|生成))",
    re.IGNORECASE,
)
_COMPLETION_EVIDENCE_RE = re.compile(
    r"(?:\b(?:completed|finished|succeeded|saved|created|verified|exit code)\b|"
    r"(?:已经|已完成|已写入|已保存|已创建|已验证|成功|退出码))",
    re.IGNORECASE,
)


def _bounded_tool_action_note(text: str, *, max_chars: int = 900, max_steps: int = 4) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""

    selected = [lines[0]]
    previous = lines[0].casefold()
    plan_mode = bool(_PLAN_HEADER_RE.search(lines[0])) or bool(_PLAN_LINE_RE.match(lines[0]))
    steps = 1 if _PLAN_LINE_RE.match(lines[0]) else 0

    for line in lines[1:]:
        normalized = line.casefold()
        if normalized == previous:
            continue
        previous = normalized
        if not plan_mode or not _PLAN_LINE_RE.match(line) or steps >= max_steps:
            break
        candidate = "\n".join([*selected, line])
        if len(candidate) > max_chars:
            break
        selected.append(line)
        steps += 1

    note = "\n".join(selected)
    return note[:max_chars].rstrip()


def _result_has_tool_intent(result: dict[str, Any]) -> bool:
    if result.get("tool_calls"):
        return True
    content = str(result.get("content", "") or "")
    return bool(parse_xml_tool_calls(content)) or find_xml_tool_call_start(content) >= 0 or _tool_call_truncated(result)


def _looks_like_unfinished_tool_progress(result: dict[str, Any]) -> bool:
    content = str(result.get("content", "") or "").strip()
    if not content or _COMPLETION_EVIDENCE_RE.search(content):
        return False
    return bool(_UNFINISHED_PROGRESS_RE.search(content))


_MAX_NO_TOOL_CORRECTIONS = 2


def _required_tool_retry_messages(
    messages: list[dict[str, Any]],
    correction_attempt: int = 1,
) -> list[dict[str, Any]]:
    return [
        *messages,
        {
            "role": "system",
            "content": (
                f"RETRY CORRECTION {correction_attempt}/{_MAX_NO_TOOL_CORRECTIONS}: "
                "The previous response did not call a tool even though "
                "this turn requires one. Discard the previous prose. Output a concise "
                "user-visible decision brief with at most four items covering diagnosis or "
                "design choice, the key constraint/tradeoff, the exact next tool action, and "
                "the validation evidence. Then immediately emit one valid "
                "XML tool call and stop. Do not apologize, promise future work, report a "
                "result, or answer without the tool call."
            ),
        },
    ]


def _responses_stream_events(
    client: FlowithClient,
    messages: list[dict[str, Any]],
    requested_model: str,
    upstream_model: str | None,
    max_tokens: int | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    require_tool: bool = False,
    correct_no_tool_progress: bool = False,
    thinking: bool | None = None,
    thinking_budget_tokens: int | None = None,
    stop_sequences: list[str] | str | None = None,
) -> Generator[bytes, None, None]:
    response_id = "resp_" + new_message_id()[4:]
    message_id = "msg_" + new_message_id()[4:]
    now = int(time.time())
    q: "queue.Queue[Any]" = queue.Queue(maxsize=512)
    result_holder: dict[str, Any] = {}
    cancel_event = threading.Event()

    def _safe_put(item: Any) -> None:
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

    def worker() -> None:
        def _call(
            call_messages: list[dict[str, Any]],
            chunk_callback: Callable[[str], None],
            reasoning_callback: Callable[[str], None],
        ) -> dict[str, Any]:
            return client.call_api(
                call_messages,
                model=upstream_model,
                max_retries=1,
                stream=True,
                on_chunk=chunk_callback,
                on_reasoning=reasoning_callback,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                tools=None,
                tool_choice=None,
                thinking=thinking,
                thinking_budget_tokens=thinking_budget_tokens,
                stop_sequences=stop_sequences,
                cancel_event=cancel_event,
            )

        try:
            if not require_tool and not correct_no_tool_progress:
                result_holder["result"] = _call(messages, on_chunk, on_reasoning)
                return

            selected_events: list[tuple[str, str]] = []

            def _capture_text(piece: str) -> None:
                if piece:
                    selected_events.append(("text", piece))

            def _capture_reasoning(piece: str) -> None:
                if piece:
                    selected_events.append(("reasoning", piece))

            call_messages = messages
            result: dict[str, Any] = {}
            for attempt in range(_MAX_NO_TOOL_CORRECTIONS + 1):
                selected_events = []
                result = _call(call_messages, _capture_text, _capture_reasoning)
                needs_correction = (
                    result.get("success")
                    and not _result_has_tool_intent(result)
                    and (require_tool or _looks_like_unfinished_tool_progress(result))
                )
                if not needs_correction:
                    break
                if attempt >= _MAX_NO_TOOL_CORRECTIONS:
                    selected_events = []
                    result = {
                        "success": False,
                        "error": (
                            "Model did not produce the required tool call after "
                            f"{_MAX_NO_TOOL_CORRECTIONS} corrections"
                        ),
                        "content": "",
                        "reasoning_content": "",
                        "usage": result.get("usage", {}) or {},
                    }
                    break
                call_messages = _required_tool_retry_messages(
                    messages,
                    correction_attempt=attempt + 1,
                )

            for kind, piece in selected_events:
                _safe_put((kind, piece))
            result_holder["result"] = result
        except Exception as e:
            result_holder["result"] = {"success": False, "error": str(e)}
        finally:
            _safe_put(_SENTINEL_DONE)

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
    tool_feedback_mode = has_tools and _is_gpt_5_6_model(requested_model, upstream_model)
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
    resp_think_in = False
    resp_think_buffer = ""
    text_done_emitted = False
    emitted_function_calls: list[dict[str, Any]] = []
    completed_items: dict[int, dict[str, Any]] = {}
    next_output_index = 0
    text_output_index: int | None = None
    raw_stream_chunks = 0
    streamed_text_chunks = 0
    pending_tool_text = ""

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
        final_event_text = "" if FLOWITH_RESPONSES_COMPACT_FINAL_TEXT else reported_text
        output_index = text_output_index if text_output_index is not None else 0
        _trace_hermes(f"responses_stream_event trace={trace_id} event=response.output_text.done text_len={len(reported_text)}")
        yield _openai_sse(
            "response.output_text.done",
            {
                "type": "response.output_text.done",
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "text": final_event_text,
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.content_part.done",
            {
                "type": "response.content_part.done",
                "item_id": message_id,
                "output_index": output_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": final_event_text, "annotations": []},
            },
        ).encode("utf-8")
        message_item = {
            "id": message_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": final_event_text, "annotations": []}],
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

    def _emit_pending_tool_note() -> Generator[bytes, None, None]:
        nonlocal pending_tool_text
        note = _bounded_tool_action_note(pending_tool_text)
        pending_tool_text = ""
        if not note:
            return
        yield from _emit_text_delta(note)
        yield from _emit_text_done()

    def _emit_response_segments(segments: list[tuple[str, Any]]) -> Generator[bytes, None, None]:
        nonlocal pending_tool_text
        for kind, payload in segments:
            if kind == "text":
                if tool_feedback_mode:
                    if not emitted_function_calls:
                        pending_tool_text += str(payload)
                    continue
                yield from _emit_text_delta(str(payload))
                continue
            if kind == "tools":
                function_calls = [_responses_function_call_item(tool) for tool in payload]
                _trace_hermes(
                    f"responses_stream_tool_mode trace={trace_id} function_calls={len(function_calls)}"
                )
                if tool_feedback_mode and function_calls and not emitted_function_calls:
                    yield from _emit_pending_tool_note()
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
        final_event_text = "" if FLOWITH_RESPONSES_COMPACT_FINAL_TEXT else reported
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
                "text": final_event_text,
            },
        ).encode("utf-8")
        yield _openai_sse(
            "response.reasoning_summary_part.done",
            {
                "type": "response.reasoning_summary_part.done",
                "item_id": reasoning_item_id,
                "output_index": output_index,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": final_event_text},
            },
        ).encode("utf-8")
        reasoning_item = {
            "id": reasoning_item_id,
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": final_event_text}],
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

    try:
        while True:
            try:
                item = q.get(timeout=_SSE_HEARTBEAT_INTERVAL)
            except queue.Empty:
                progress = {
                    "type": "response.in_progress",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": now,
                        "status": "in_progress",
                        "model": requested_model,
                        "output": [],
                    },
                }
                yield _openai_sse("response.in_progress", progress).encode("utf-8")
                continue
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
            resp_think_buffer += str(payload)
            resp_think_segments, resp_think_buffer, resp_think_in = _split_stream_think_segments(
                resp_think_buffer, resp_think_in
            )
            for seg_kind, seg_text in resp_think_segments:
                if not seg_text:
                    continue
                if seg_kind == "reasoning":
                    yield from _emit_reasoning_delta(seg_text)
                    continue
                if has_tools:
                    xml_buffer += seg_text
                    segments, xml_buffer = _drain_xml_tool_stream_buffer(xml_buffer)
                    yield from _emit_response_segments(segments)
                    continue
                yield from _emit_text_delta(seg_text)
    finally:
        cancel_event.set()

    if resp_think_buffer and not resp_think_in:
        if has_tools:
            xml_buffer += resp_think_buffer
        else:
            yield from _emit_text_delta(resp_think_buffer)
        resp_think_buffer = ""

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
        error_message = str(result.get("error", "upstream error"))
        if tool_feedback_mode and pending_tool_text and not emitted_function_calls:
            yield from _emit_text_delta(pending_tool_text)
            pending_tool_text = ""
        yield from _emit_reasoning_done()
        yield from _emit_text_done()
        completed_output = [completed_items[index] for index in sorted(completed_items)]
        yield _openai_sse(
            "response.failed",
            {
                "type": "response.failed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": now,
                    "status": "failed",
                    "model": requested_model,
                    "output": completed_output,
                    "output_text": "" if FLOWITH_RESPONSES_COMPACT_FINAL_TEXT else output_text,
                    "error": {"type": "upstream_error", "message": error_message},
                },
            },
        ).encode("utf-8")
        yield _openai_sse(None, "[DONE]").encode("utf-8")
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
        if tool_feedback_mode and not emitted_function_calls:
            yield from _emit_pending_tool_note()
        for function_call in native_function_calls:
            yield from _emit_function_call(function_call)

    if tool_feedback_mode and pending_tool_text and not emitted_function_calls:
        yield from _emit_text_delta(pending_tool_text)
        pending_tool_text = ""

    # Finalize reasoning first; if upstream only returned reasoning at the end (non-stream fallback),
    # emit whatever accumulated in result.reasoning_content.
    final_reasoning = result.get("reasoning_content", "") or ""
    if final_reasoning and not reasoning_item_started:
        yield from _emit_reasoning_delta(final_reasoning)
    yield from _emit_reasoning_done(
        final_text=final_reasoning if final_reasoning else None
    )

    # If the upstream did NOT stream any text chunks but returned a final content
    # string (non-streaming or coalesced-response backends), emit it here so the
    # codex CLI receives the answer instead of an empty message. Skip for the
    # tool-mode path where the payload is already routed through function_calls.
    if not has_tools and not emitted_function_calls:
        final_content = result.get("content", "") or ""
        if final_content and not output_text:
            _trace_hermes(
                f"responses_stream_flush_final_content trace={trace_id} len={len(final_content)}"
            )
            yield from _emit_text_delta(final_content)

    if not text_item_started and not emitted_function_calls:
        yield from _emit_text_start()

    yield from _emit_text_done()

    completed_output = [completed_items[index] for index in sorted(completed_items)]

    truncated = _tool_call_truncated(result)
    completed_mode = "function_calls" if emitted_function_calls else "text"
    event_type = "response.incomplete" if truncated else "response.completed"
    _trace_hermes(f"responses_stream_event trace={trace_id} event={event_type} mode={completed_mode}")
    response_payload: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": now,
        "status": "incomplete" if truncated else "completed",
        "model": requested_model,
        "output": completed_output,
        "output_text": "" if FLOWITH_RESPONSES_COMPACT_FINAL_TEXT else output_text,
        "usage": usage,
    }
    if truncated:
        response_payload["incomplete_details"] = {"reason": "max_output_tokens"}
    yield _openai_sse(
        event_type,
        {
            "type": event_type,
            "response": response_payload,
        },
    ).encode("utf-8")
    yield _openai_sse(None, "[DONE]").encode("utf-8")
