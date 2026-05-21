"""Anthropic Claude <-> Flowith format adapter.

This module is pure (no I/O); it only translates JSON shapes and
builds SSE event strings.

Claude API reference (https://docs.anthropic.com/en/api/messages):
  - POST /v1/messages, body:
      {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 1024,
        "system": "You are ...",   # optional, string or list of blocks
        "messages": [
          {"role": "user", "content": "hi"},
          {"role": "assistant", "content": [{"type":"text","text":"..."}]},
        ],
        "stream": true | false
      }
  - Non-stream response:
      {
        "id": "msg_xxx", "type": "message", "role": "assistant",
        "model": "...",
        "content": [{"type":"text","text":"..."}],
        "stop_reason": "end_turn", "stop_sequence": null,
        "usage": {"input_tokens": N, "cache_creation_input_tokens": 0,
                  "cache_read_input_tokens": 0, "output_tokens": M}
      }
  - Stream event order:
      message_start -> ping -> content_block_start
         -> content_block_delta * N
         -> content_block_stop -> message_delta -> message_stop
"""

from __future__ import annotations

import json
import uuid
from typing import Any

# ---------------------------------------------------------------
# Model alias table: Claude-style names -> Flowith model ids.
# Unknown ids pass through unchanged.
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
# Claude content blocks -> plain text
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


# ---------------------------------------------------------------
# Claude request -> Flowith messages
# ---------------------------------------------------------------
def claude_request_to_flowith_messages(body: dict[str, Any]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []

    system = body.get("system")
    if system:
        sys_text = _extract_text(system)
        if sys_text:
            messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages", []) or []:
        role = msg.get("role", "user")
        text = _extract_text(msg.get("content"))
        if role not in ("system", "user", "assistant"):
            role = "user"
        messages.append({"role": role, "content": text})

    return messages


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
    usage = flowith_result.get("usage", {}) or {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    return {
        "id": new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": [{"type": "text", "text": content_text}],
        "stop_reason": "end_turn",
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


def sse_content_block_start(index: int = 0) -> str:
    return _sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "text", "text": ""},
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
