"""Anthropic Claude <-> Flowith (OpenAI-compatible) format adapter.

This module is pure (no I/O); it only translates JSON shapes and
builds SSE event strings.

Key capabilities:
  - Model alias mapping
  - Anthropic tools -> OpenAI tools conversion
  - Anthropic tool_use/tool_result messages -> OpenAI tool_calls/tool messages
  - OpenAI tool_calls response -> Anthropic tool_use content blocks
  - Full SSE event builders including tool_use streaming
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# ---------------------------------------------------------------
# Model alias table: Claude-style names -> Flowith model ids.
# ---------------------------------------------------------------
MODEL_ALIASES: dict[str, str] = {
    "claude-3-5-sonnet-20241022": "claude-4.6-sonnet",
    "claude-3-5-sonnet-latest": "claude-4.6-sonnet",
    "claude-3-5-sonnet": "claude-4.6-sonnet",
    "claude-3-7-sonnet-20250219": "claude-4.6-sonnet",
    "claude-3-7-sonnet-latest": "claude-4.6-sonnet",
    "claude-sonnet-4-20250514": "claude-4.6-sonnet",
    "claude-sonnet-4-5": "claude-4.6-sonnet",
    "claude-opus-4-20250514": "claude-opus-4.7",
    "claude-opus-4-1": "claude-opus-4.7",
    "claude-opus-4-5": "claude-opus-4.7",
    "claude-3-opus-20240229": "claude-opus-4.7",
    "claude-3-haiku-20240307": "claude-4.6-sonnet",
}


def map_model(claude_model: str, default: str = "claude-4.6-sonnet") -> str:
    if not claude_model:
        return default
    from .config import CUSTOM_MODEL_ALIASES
    merged = {**MODEL_ALIASES, **CUSTOM_MODEL_ALIASES}
    if claude_model in merged:
        return merged[claude_model]
    if claude_model.startswith(("gpt-", "gemini-", "claude-")):
        return claude_model
    return default


# ---------------------------------------------------------------
# Tool schema conversion: Anthropic -> OpenAI
# ---------------------------------------------------------------
def anthropic_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") == "computer_20250124":
            continue
        name = tool.get("name", "")
        description = tool.get("description", "")
        parameters = tool.get("input_schema", {})
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters,
            },
        })
    return result


def anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    if tool_choice is None or tool_choice == "auto":
        return "auto"
    if tool_choice == "any":
        return "required"
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "tool":
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return "auto"


# ---------------------------------------------------------------
# Content block helpers
# ---------------------------------------------------------------
def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", "") or "")
                elif btype == "tool_use":
                    parts.append(
                        f"[tool_use name={block.get('name')} "
                        f"input={json.dumps(block.get('input', {}), ensure_ascii=False)}]"
                    )
                elif btype == "tool_result":
                    parts.append(
                        f"[tool_result]\n{_extract_text(block.get('content', ''))}"
                    )
                elif btype == "image":
                    parts.append("[image omitted]")
        return "\n".join(p for p in parts if p)
    return str(content)


def _has_tool_blocks(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") in ("tool_use", "tool_result")
        for b in content
    )


# ---------------------------------------------------------------
# Claude request -> Flowith (OpenAI-compatible) messages
# ---------------------------------------------------------------
def claude_request_to_flowith_messages(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    system = body.get("system")
    if system:
        sys_text = _extract_text(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages", []) or []:
        role = msg.get("role", "user")
        content = msg.get("content")

        if role not in ("system", "user", "assistant"):
            role = "user"

        # Structured tool messages -> OpenAI format
        if isinstance(content, list) and _has_tool_blocks(content):
            messages.extend(_convert_tool_messages(role, content))
        else:
            text = _extract_text(content)
            messages.append({"role": role, "content": text})

    return messages


def _convert_tool_messages(
    role: str, content: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for block in content:
        if not isinstance(block, dict):
            text_parts.append(str(block))
            continue

        btype = block.get("type")

        if btype == "text":
            text_parts.append(block.get("text", "") or "")

        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(
                        block.get("input", {}), ensure_ascii=False
                    ),
                },
            })

        elif btype == "tool_result":
            # Flush any pending assistant content first
            if text_parts or tool_calls:
                result.append(_build_assistant_msg(text_parts, tool_calls))
                text_parts = []
                tool_calls = []

            result.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": _extract_text(block.get("content", "")),
            })

    if text_parts or tool_calls:
        result.append(_build_assistant_msg(text_parts, tool_calls))

    return result


def _build_assistant_msg(
    text_parts: list[str], tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    combined = "\n".join(p for p in text_parts if p)
    msg["content"] = combined or None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ---------------------------------------------------------------
# OpenAI tool_calls -> Anthropic content blocks
# ---------------------------------------------------------------
def openai_tool_calls_to_anthropic(
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for tc in tool_calls:
        func = tc.get("function", {})
        name = func.get("name", "")
        raw_args = func.get("arguments", "{}")
        try:
            input_data = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except (json.JSONDecodeError, TypeError):
            input_data = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
            "name": name,
            "input": input_data,
        })
    return blocks


# ---------------------------------------------------------------
# Flowith result -> Claude non-streaming response
# ---------------------------------------------------------------
def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def flowith_result_to_claude_response(
    flowith_result: dict[str, Any],
    requested_model: str,
) -> dict[str, Any]:
    content_text = flowith_result.get("content", "") or ""
    tool_calls = flowith_result.get("tool_calls") or []
    usage = flowith_result.get("usage", {}) or {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    content: list[dict[str, Any]] = []
    if content_text:
        content.append({"type": "text", "text": content_text})
    if tool_calls:
        content.extend(openai_tool_calls_to_anthropic(tool_calls))

    if not content:
        content.append({"type": "text", "text": ""})

    stop_reason = "tool_use" if tool_calls else "end_turn"

    return {
        "id": new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": output_tokens,
        },
    }


# ---------------------------------------------------------------
# Claude SSE event builders
# ---------------------------------------------------------------
def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_message_start(message_id: str, requested_model: str, input_tokens: int = 0) -> str:
    return _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": int(input_tokens),
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "output_tokens": 0,
                },
            },
        },
    )


def sse_content_block_start(index: int = 0, block_type: str = "text", tool_id: str = "", tool_name: str = "") -> str:
    content_block: dict[str, Any] = {"type": block_type}
    if block_type == "text":
        content_block["text"] = ""
    elif block_type == "tool_use":
        content_block["id"] = tool_id
        content_block["name"] = tool_name
        content_block["input"] = {}
    return _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": content_block,
        },
    )


def sse_content_block_delta(text: str, index: int = 0) -> str:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    )


def sse_tool_input_delta(partial_json: str, index: int = 0) -> str:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        },
    )


def sse_content_block_stop(index: int = 0) -> str:
    return _sse(
        "content_block_stop",
        {"type": "content_block_stop", "index": index},
    )


def sse_message_delta(output_tokens: int = 0, stop_reason: str = "end_turn") -> str:
    return _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": int(output_tokens)},
        },
    )


def sse_message_stop() -> str:
    return _sse("message_stop", {"type": "message_stop"})


def sse_ping() -> str:
    return _sse("ping", {"type": "ping"})


def sse_error(message: str, err_type: str = "api_error") -> str:
    return _sse(
        "error",
        {"type": "error", "error": {"type": err_type, "message": message}},
    )
