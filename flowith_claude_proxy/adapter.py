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
import re
import uuid
from typing import Any

# ---------------------------------------------------------------
# Available Flowith upstream models and client-facing aliases.
# Custom overrides via FLOWITH_MODEL_ALIASES env var.
# Any model name containing "-" or "/" is passed through as-is.
# ---------------------------------------------------------------
MODEL_ALIASES: dict[str, str] = {
    # Upstream identity mappings
    "claude-4.6-sonnet": "claude-4.6-sonnet",
    "claude-opus-4.7": "claude-opus-4.7",
    "claude-haiku-4.5": "claude-haiku-4.5",
    "gpt-5.5": "gpt-5.5",
    "gpt-5.4": "gpt-5.4",
    "gemini-2.5-pro": "gemini-2.5-pro",
    # Client-facing aliases -> upstream models
    "claude-sonnet-4.6": "claude-4.6-sonnet",
    "claude-sonnet-4-20250514": "claude-4.6-sonnet",
}


def map_model(claude_model: str, default: str = "claude-4.6-sonnet") -> str:
    if not claude_model:
        return default
    from .config import CUSTOM_MODEL_ALIASES
    merged = {**MODEL_ALIASES, **CUSTOM_MODEL_ALIASES}
    if claude_model in merged:
        return merged[claude_model]
    # Pass through any model id that looks like a real model name
    # (contains a dash or slash, typical of provider/model patterns)
    if "/" in claude_model or "-" in claude_model:
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
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "tool":
            return {"type": "function", "function": {"name": tool_choice["name"]}}
        if tc_type == "any":
            return "required"
        # "auto" or unknown dict → let model decide
        return "auto"
    if tool_choice == "auto":
        return "auto"
    if tool_choice == "any":
        return "required"
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
def _build_tool_xml_prompt(anthropic_tools: list[dict[str, Any]]) -> str:
    """Generate XML-format tool-use instructions from Anthropic tool definitions.

    Injects into the system prompt so the upstream model (which may not
    support native function calling) knows to output <function_calls> XML
    with CDATA-wrapped parameter values for safe parsing.
    """
    if not anthropic_tools:
        return ""

    tool_descs: list[str] = []
    for tool in anthropic_tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        schema = tool.get("input_schema", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])

        param_parts: list[str] = []
        for pname, pinfo in props.items():
            ptype = pinfo.get("type", "string")
            pdesc = pinfo.get("description", "")
            is_req = "required" if pname in required else "optional"
            param_parts.append(f"    {pname} ({ptype}, {is_req}): {pdesc}")

        param_str = "\n".join(param_parts) if param_parts else "    (no parameters)"
        tool_descs.append(
            f"### {name}\n{desc}\nParameters:\n{param_str}"
        )

    tool_list = "\n\n".join(tool_descs)

    return (
        "\n\n## Tool Use Instructions\n\n"
        "You have access to the following tools. To use a tool, output it "
        "in this EXACT XML format inside your response:\n\n"
        "<function_calls>\n"
        "<invoke name=\"TOOL_NAME\">\n"
        "<parameter name=\"PARAM_NAME\"><![CDATA[PARAM_VALUE]]></parameter>\n"
        "</invoke>\n"
        "</function_calls>\n\n"
        "CRITICAL parameter formatting rules:\n"
        "- ALWAYS wrap every parameter value in <![CDATA[...]]>, even for "
        "simple values. This is MANDATORY — never output a raw parameter "
        "value without CDATA.\n"
        "- Simple strings: <parameter name=\"cmd\"><![CDATA[ls -la]]></parameter>\n"
        "- Numbers/booleans: <parameter name=\"n\"><![CDATA[42]]></parameter>\n"
        "- JSON objects/arrays: <parameter name=\"filters\"><![CDATA[{\"key\":\"val\"}]]></parameter>\n"
        "- Multi-line or special-char content (code, shell commands, paths "
        "with backslashes, XML/HTML): always safe inside CDATA.\n"
        "- NEVER output <function_calls> XML unless you are actually "
        "calling a tool.\n"
        "- Each <function_calls> block can contain multiple <invoke> elements.\n"
        "- After outputting tool calls, STOP — the tool result will be provided.\n\n"
        f"{tool_list}\n"
    )


def claude_request_to_flowith_messages(
    body: dict[str, Any],
    anthropic_tools: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    system = body.get("system")
    sys_text = _extract_text(system) if system else ""

    # Inject XML tool-use instructions so the upstream model knows how to
    # call tools even when it doesn't support native function calling.
    if anthropic_tools:
        xml_prompt = _build_tool_xml_prompt(anthropic_tools)
        if xml_prompt:
            sys_text = (sys_text + xml_prompt) if sys_text else xml_prompt

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

            # Flowith may not accept role="tool"; use user message with
            # structured markers the model can recognise.
            tool_id = block.get("tool_use_id", "")
            is_error = block.get("is_error", False)
            result_text = _extract_text(block.get("content", ""))
            status_tag = " error" if is_error else ""
            result.append({
                "role": "user",
                "content": (
                    f"<tool_result id=\"{tool_id}\"{status_tag}>\n"
                    f"{result_text}\n"
                    f"</tool_result>"
                ),
            })

    if text_parts or tool_calls:
        result.append(_build_assistant_msg(text_parts, tool_calls))

    return result


def _build_assistant_msg(
    text_parts: list[str], tool_calls: list[dict[str, Any]]
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    combined = "\n".join(p for p in text_parts if p)
    msg["content"] = combined or ""
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


# ---------------------------------------------------------------
# XML tool call parser (robust position-based, CDATA-aware)
# ---------------------------------------------------------------
# Previous approach used non-greedy regex which breaks when parameter
# values contain XML special characters (e.g. shell redirects, HTML).
# This parser uses string.find + CDATA extraction, falling back to
# regex patterns only for malformed XML that misses CDATA wrappers.

_TAG_FUNC_OPEN = "<function_calls>"
_TAG_FUNC_CLOSE = "</function_calls>"
_TAG_INVOKE_OPEN = "<invoke"
_TAG_INVOKE_CLOSE = "</invoke>"
_TAG_PARAM_OPEN = "<parameter"
_TAG_PARAM_CLOSE = "</parameter>"
_CDATA_START = "<![CDATA["
_CDATA_END = "]]>"
_INVOKE_NAME_RE = re.compile(r'name="([^"]*)"')
_PARAM_NAME_RE = re.compile(r'name="([^"]*)"')


def _find_outside_cdata(text: str, needle: str, start: int = 0) -> int:
    """Find *needle* in *text* skipping any occurrence that lies inside a
    CDATA section.  Returns -1 when no CDATA-free match exists."""
    pos = start
    while True:
        idx = text.find(needle, pos)
        if idx == -1:
            return -1
        # Check whether idx lies inside a CDATA section.
        # Search from position 0 (not *start*) to cover CDATA that
        # opened before the search window but still spans across idx.
        cdata_start = text.rfind(_CDATA_START, 0, idx)
        if cdata_start != -1:
            cdata_end = text.find(_CDATA_END, cdata_start)
            if cdata_end != -1 and idx < cdata_end:
                # Match is inside CDATA — skip past the CDATA section
                pos = cdata_end + len(_CDATA_END)
                continue
        return idx


def _rfind_outside_cdata(text: str, needle: str, start: int = 0, end: int | None = None) -> int:
    """Like rfind, but skip occurrences that lie inside a CDATA section."""
    if end is None:
        end = len(text)
    pos = end
    while True:
        idx = text.rfind(needle, start, pos)
        if idx == -1:
            return -1
        cdata_start = text.rfind(_CDATA_START, 0, idx)
        if cdata_start != -1:
            cdata_end = text.find(_CDATA_END, cdata_start)
            if cdata_end != -1 and idx < cdata_end:
                # Match is inside CDATA — search before this CDATA section
                pos = cdata_start
                continue
        return idx


def _extract_cdata_or_text(raw: str) -> str:
    """Extract value from CDATA or raw text with XML entity unescaping."""
    stripped = raw.strip()
    if stripped.startswith(_CDATA_START) and stripped.endswith(_CDATA_END):
        return stripped[len(_CDATA_START):-len(_CDATA_END)]
    import html
    return html.unescape(stripped)


def _coerce_param_value(raw: str) -> Any:
    """Try JSON parse, fall back to string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _parse_parameter_elements(body: str) -> dict[str, Any]:
    """Parse <parameter name=\"...\">...</parameter> from an invoke body.

    Handles CDATA-wrapped values (preferred) and raw text (fallback).
    Uses CDATA-aware search so that tags appearing inside CDATA sections
    are never mistaken for structural markers.
    """
    params: dict[str, Any] = {}
    pos = 0
    while True:
        tag_start = body.find(_TAG_PARAM_OPEN, pos)
        if tag_start == -1:
            break
        # Extract name attribute from opening tag
        tag_end = body.find(">", tag_start)
        if tag_end == -1:
            break
        name_m = _PARAM_NAME_RE.search(body, tag_start, tag_end)
        if not name_m:
            pos = tag_end + 1
            continue
        p_name = name_m.group(1)
        # Find matching close tag (skip any </parameter> inside CDATA)
        close_start = _find_outside_cdata(body, _TAG_PARAM_CLOSE, tag_end + 1)
        if close_start == -1:
            break
        raw_value = body[tag_end + 1:close_start]
        p_value = _coerce_param_value(_extract_cdata_or_text(raw_value))
        params[p_name] = p_value
        pos = close_start + len(_TAG_PARAM_CLOSE)
    return params


def _parse_invoke_elements(func_body: str, results: list[dict[str, Any]]) -> None:
    """Parse <invoke name=\"...\"> elements from a <function_calls> body.

    Uses CDATA-aware search so that </invoke> inside a CDATA section is
    never mistaken for a structural close-tag.
    """
    pos = 0
    while True:
        tag_start = func_body.find(_TAG_INVOKE_OPEN, pos)
        if tag_start == -1:
            break
        tag_end = func_body.find(">", tag_start)
        if tag_end == -1:
            break
        name_m = _INVOKE_NAME_RE.search(func_body, tag_start, tag_end)
        if not name_m:
            pos = tag_end + 1
            continue
        tool_name = name_m.group(1)
        # Find matching </invoke> (skip any occurrence inside CDATA)
        close_start = _find_outside_cdata(func_body, _TAG_INVOKE_CLOSE, tag_end + 1)
        if close_start == -1:
            break
        invoke_body = func_body[tag_end + 1:close_start]
        params = _parse_parameter_elements(invoke_body)
        results.append({
            "id": f"toolu_{uuid.uuid4().hex[:24]}",
            "name": tool_name,
            "input": params,
        })
        pos = close_start + len(_TAG_INVOKE_CLOSE)


def parse_xml_tool_calls(text: str) -> list[dict[str, Any]]:
    """Parse Flowith-style XML tool calls with CDATA support.

    Returns a list of dicts with keys: id, name, input.
    Handles both CDATA-wrapped parameters and raw text (fallback).
    Uses CDATA-aware search so structural tags inside CDATA values
    (e.g. shell commands echoing XML-like strings) don't break parsing.
    """
    if _TAG_FUNC_OPEN not in text:
        return []

    results: list[dict[str, Any]] = []
    pos = 0
    while True:
        start = text.find(_TAG_FUNC_OPEN, pos)
        if start == -1:
            break
        content_start = start + len(_TAG_FUNC_OPEN)
        end = _find_outside_cdata(text, _TAG_FUNC_CLOSE, content_start)
        if end == -1:
            break
        func_body = text[content_start:end]
        _parse_invoke_elements(func_body, results)
        pos = end + len(_TAG_FUNC_CLOSE)
    return results


def split_text_and_xml_tool_calls(text: str) -> list[dict[str, Any]]:
    """Split text into Anthropic content blocks, extracting XML tool calls.

    Returns a list of content blocks: {"type": "text", "text": ...} and
    {"type": "tool_use", "id": ..., "name": ..., "input": ...}.
    Uses the same robust CDATA-aware parser as parse_xml_tool_calls.
    """
    if _TAG_FUNC_OPEN not in text:
        return [{"type": "text", "text": text}] if text else []

    blocks: list[dict[str, Any]] = []
    last_end = 0
    pos = 0

    while True:
        start = text.find(_TAG_FUNC_OPEN, pos)
        if start == -1:
            break
        # Text before this function_calls block
        pre_text = text[last_end:start].strip()
        if pre_text:
            blocks.append({"type": "text", "text": pre_text})

        content_start = start + len(_TAG_FUNC_OPEN)
        end = _find_outside_cdata(text, _TAG_FUNC_CLOSE, content_start)
        if end == -1:
            break
        func_body = text[content_start:end]
        # Parse tool calls from this block
        tool_results: list[dict[str, Any]] = []
        _parse_invoke_elements(func_body, tool_results)
        # Add type marker for content-block compatibility
        for tr in tool_results:
            tr["type"] = "tool_use"
        blocks.extend(tool_results)

        pos = end + len(_TAG_FUNC_CLOSE)
        last_end = end + len(_TAG_FUNC_CLOSE)

    # Trailing text after all blocks
    post_text = text[last_end:].strip()
    if post_text:
        blocks.append({"type": "text", "text": post_text})

    return blocks


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
    reasoning_text = flowith_result.get("reasoning_content", "") or ""
    tool_calls = flowith_result.get("tool_calls") or []
    usage = flowith_result.get("usage", {}) or {}
    finish_reason = flowith_result.get("finish_reason") or ""
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    content: list[dict[str, Any]] = []
    if reasoning_text:
        content.append({"type": "thinking", "thinking": reasoning_text})

    # Check for XML tool calls in content text (Flowith returns tool calls as XML).
    # Use CDATA-aware search so literal <function_calls> inside a parameter
    # value is not mistaken for a structural block.
    has_xml_tool_calls = _find_outside_cdata(content_text, "<function_calls>") != -1

    if has_xml_tool_calls:
        content.extend(split_text_and_xml_tool_calls(content_text))
    elif content_text:
        content.append({"type": "text", "text": content_text})

    if tool_calls:
        content.extend(openai_tool_calls_to_anthropic(tool_calls))

    if not content:
        content.append({"type": "text", "text": ""})

    # Map OpenAI finish_reason to Anthropic stop_reason
    has_any_tool_use = bool(tool_calls) or any(
        b.get("type") == "tool_use" for b in content
    )
    if has_any_tool_use:
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
    elif finish_reason == "content_filter":
        stop_reason = "end_turn"  # closest meaningful equivalent
    else:
        stop_reason = "end_turn"

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
    if block_type == "thinking":
        content_block["thinking"] = ""
    elif block_type == "text":
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


def sse_thinking_delta(thinking: str, index: int = 0) -> str:
    return _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": thinking},
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
