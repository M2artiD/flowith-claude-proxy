"""Anthropic Claude <-> Flowith (OpenAI-compatible) format adapter.

Pure conversion — no I/O. Handles:
  - Model alias mapping
  - Anthropic tools -> OpenAI tools
  - tool_use/tool_result messages -> OpenAI tool_calls/tool messages
  - OpenAI tool_calls -> Anthropic tool_use content blocks
  - XML tool call parsing (CDATA-aware) for models without native function calling
  - Full SSE event builders including tool_use streaming
"""

from __future__ import annotations

import html
import json
import re
import uuid
from typing import Any

REACT_TOOL_STOP_SEQUENCE = "</tool_call>"

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


# NOTE on <think> filtering:
#   The streaming path (server.py / codex/router.py: filter_stream_think_tags)
#   has its own state machine that strips <think>...</think> spans token by
#   token. That logic is authoritative for streaming and must NOT be touched
#   here.
#
#   For the non-streaming path we only strip *paired* <think>...</think> blocks.
#   A previous fallback regex eagerly deleted everything from any lone
#   <think> tag in prose (e.g. when the user/model is literally
#   talking about the word "think" tags), the whole tail of the answer was
#   silently swallowed. Real unclosed thinking blocks in non-streaming
#   responses are vanishingly rare; preserving prose is the safer default.
def strip_model_thinking(text: str) -> str:
    """Remove paired <think>...</think> blocks from buffered assistant text."""
    if not text:
        return ""
    return _THINK_RE.sub("", text)


# ---------------------------------------------------------------
# Model aliases
# ---------------------------------------------------------------
def map_model(claude_model: str, default: str = "claude-4.6-sonnet") -> str:
    """Passthrough model name as-is. Only apply user-defined custom aliases."""
    if not claude_model:
        return default
    from .config import CUSTOM_MODEL_ALIASES
    if claude_model in CUSTOM_MODEL_ALIASES:
        return CUSTOM_MODEL_ALIASES[claude_model]
    return claude_model


# ---------------------------------------------------------------
# Tool schema conversion
# ---------------------------------------------------------------
ANTHROPIC_BUILTIN_TOOL_TYPES = {
    "computer_20241022",
    "text_editor_20241022",
    "bash_20241022",
    "computer_20250124",
    "text_editor_20250124",
    "bash_20250124",
}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    try:
        return dict(value)
    except Exception:
        return {}


def _fix_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_fix_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    fixed = {key: _fix_schema(value) for key, value in schema.items()}
    schema_type = fixed.get("type")
    is_array = schema_type == "array" or (
        isinstance(schema_type, list) and "array" in schema_type
    )
    if is_array and "items" not in fixed:
        fixed["items"] = {}
    return fixed


def _compact_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    keep = {"type", "properties", "required", "items", "enum", "description"}
    compact: dict[str, Any] = {}
    for key, value in schema.items():
        if key not in keep:
            continue
        if key == "properties" and isinstance(value, dict):
            compact[key] = {name: _compact_schema(prop) for name, prop in value.items()}
        elif key == "items":
            compact[key] = _compact_schema(value)
        elif key == "description" and isinstance(value, str):
            compact[key] = value[:300]
        else:
            compact[key] = value
    return compact


def anthropic_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw_tool in tools:
        tool = _as_dict(raw_tool)
        if tool.get("type") in ANTHROPIC_BUILTIN_TOOL_TYPES:
            continue
        name = tool.get("name", "")
        if not name:
            continue
        result.append({
            "type": "function",
            "function": {
                "name": name,
                "description": tool.get("description", ""),
                "parameters": _fix_schema(tool.get("input_schema", {}) or {}),
            },
        })
    return result


def anthropic_tool_choice_to_openai(tool_choice: Any) -> Any:
    if tool_choice is None:
        return "auto"
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "tool":
            name = tool_choice.get("name")
            return {"type": "function", "function": {"name": name}} if name else "auto"
        if tc_type == "any":
            return "required"
        if tc_type == "none":
            return "none"
        return "auto"
    if tool_choice == "auto":
        return "auto"
    if tool_choice == "any":
        return "required"
    if tool_choice == "none":
        return "none"
    return "auto"


# ---------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------
def _extract_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for raw_block in content:
            block = _as_dict(raw_block) if not isinstance(raw_block, str) else raw_block
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
        isinstance(_as_dict(b), dict) and _as_dict(b).get("type") in ("tool_use", "tool_result")
        for b in content
    )


# ---------------------------------------------------------------
# Claude request -> Flowith messages
# ---------------------------------------------------------------
def _build_tool_xml_prompt_legacy(anthropic_tools: list[dict[str, Any]]) -> str:
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
        tool_descs.append(f"### {name}\n{desc}\nParameters:\n{param_str}")

    tool_list = "\n\n".join(tool_descs)

    return (
        "<SYSTEM_OVERRIDE>\n"
        "YOU ARE A CODING ASSISTANT. YOU MUST USE TOOLS TO COMPLETE TASKS.\n"
        "Do NOT say you cannot run commands — you CAN by outputting tool calls.\n"
        "When the user asks you to do something that requires a tool, you MUST\n"
        "output the EXACT XML below instead of refusing or explaining:\n\n"
        "<function_calls>\n"
        "<invoke name=\"TOOL_NAME\">\n"
        "<parameter name=\"PARAM_NAME\"><![CDATA[PARAM_VALUE]]></parameter>\n"
        "</invoke>\n"
        "</function_calls>\n\n"
        "CRITICAL RULES:\n"
        "- ALWAYS output XML tool calls when the user asks for actions.\n"
        "- Each parameter value MUST be wrapped in <![CDATA[...]]>.\n"
        "- After </function_calls>, STOP immediately — do NOT invent results.\n"
        "- You can invoke multiple tools in one <function_calls> block.\n\n"
        f"AVAILABLE TOOLS:\n{tool_list}\n"
        "</SYSTEM_OVERRIDE>"
    )


def _tool_choice_instruction(tool_choice: Any) -> str:
    if isinstance(tool_choice, dict):
        tc_type = tool_choice.get("type")
        if tc_type == "tool" and tool_choice.get("name"):
            return f'You must call the "{tool_choice["name"]}" tool.'
        if tc_type == "any":
            return "You must call one available tool."
        if tc_type == "none":
            return "Do not call tools for this response."
    elif tool_choice == "any":
        return "You must call one available tool."
    elif tool_choice == "none":
        return "Do not call tools for this response."
    return "Call a tool whenever it is needed to answer or act correctly."


def _build_tool_xml_prompt(
    anthropic_tools: list[dict[str, Any]],
    tool_choice: Any = None,
) -> str:
    if not anthropic_tools:
        return ""

    tool_descs: list[str] = []
    for raw_tool in anthropic_tools:
        tool = _as_dict(raw_tool)
        if tool.get("type") in ANTHROPIC_BUILTIN_TOOL_TYPES:
            continue
        name = tool.get("name", "")
        if not name:
            continue
        desc = tool.get("description", "")
        schema = _compact_schema(tool.get("input_schema", {}) or {})
        schema_json = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        lines = [f"### {name}"]
        if desc:
            lines.append(desc)
        lines.append(f"Input schema: {schema_json}")
        tool_descs.append("\n".join(lines))

    if not tool_descs:
        return ""

    tool_list = "\n\n".join(tool_descs)
    choice_instruction = _tool_choice_instruction(tool_choice)

    return (
        "# TOOL CALLING - YOU MUST USE THIS EXACT XML FORMAT\n\n"
        "You are connected to Claude Code tools through this proxy. Native Anthropic "
        "tool definitions are not sent to the upstream model, so tool calls must be "
        "expressed as XML text.\n\n"
        "To call a tool, you MUST output this EXACT XML block - nothing else works:\n\n"
        "<tool_call>\n"
        "<name>TOOL_NAME</name>\n"
        "<parameters>\n"
        "{\"param1\": \"value1\"}\n"
        "</parameters>\n"
        "</tool_call>\n\n"
        "CRITICAL RULES:\n"
        "1. You MUST use the <tool_call> XML block above to call tools. Do NOT describe or narrate tool calls in plain text.\n"
        "2. Output ONLY ONE tool call per response.\n"
        "3. The <parameters> body MUST be one valid JSON object matching the input schema.\n"
        "4. STOP writing immediately after </tool_call> and wait for <observation>.\n"
        "5. Treat every <observation> block as the result of your previous tool call.\n"
        f"6. {choice_instruction}\n\n"
        "EXAMPLE - calling a tool named \"Bash\" with parameter \"command\":\n\n"
        "I need to list the files in the current directory.\n\n"
        "<tool_call>\n"
        "<name>Bash</name>\n"
        "<parameters>\n"
        "{\"command\": \"ls -la\"}\n"
        "</parameters>\n"
        "</tool_call>\n\n"
        f"## Available Tools\n\n{tool_list}"
    )


def build_tool_xml_prompt(
    anthropic_tools: list[dict[str, Any]],
    tool_choice: Any = None,
) -> str:
    return _build_tool_xml_prompt(anthropic_tools, tool_choice=tool_choice)


def claude_request_to_flowith_messages(
    body: dict[str, Any],
    anthropic_tools: list[dict[str, Any]] | None = None,
    tool_mode: str = "xml",
    tool_choice: Any = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    system = body.get("system")
    sys_text = _extract_text(system) if system else ""

    if tool_mode == "xml" and anthropic_tools:
        xml_prompt = _build_tool_xml_prompt(anthropic_tools, tool_choice=tool_choice)
        if xml_prompt:
            # PREPEND so the model sees this before Claude Code's long system prompt
            sys_text = (xml_prompt + "\n\n---\n\n" + sys_text) if sys_text else xml_prompt

    if sys_text:
        messages.append({"role": "system", "content": sys_text})

    for msg in body.get("messages", []) or []:
        role = msg.get("role", "user")
        content = msg.get("content")

        if role not in ("system", "user", "assistant"):
            role = "user"

        if isinstance(content, list) and _has_tool_blocks(content):
            if tool_mode == "native":
                messages.extend(_convert_tool_messages_native(role, content))
            else:
                messages.append(_convert_tool_messages_xml(role, content))
        else:
            text = _extract_text(content)
            messages.append({"role": role, "content": text})

    return messages


def _convert_tool_messages_legacy(
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
            if text_parts or tool_calls:
                result.append(_build_assistant_msg(text_parts, tool_calls))
                text_parts = []
                tool_calls = []

            tool_id = block.get("tool_use_id", "")
            is_error = block.get("is_error", False)
            result_text = _extract_text(block.get("content", ""))
            status_tag = " error" if is_error else ""
            result.append({
                "role": "user",
                "content": (
                    f'<tool_result id="{tool_id}"{status_tag}>\n'
                    f"{result_text}\n"
                    f"</tool_result>"
                ),
            })

    if text_parts or tool_calls:
        result.append(_build_assistant_msg(text_parts, tool_calls))

    return result


def _json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _format_tool_call_xml(name: str, arguments: Any) -> str:
    args = arguments if isinstance(arguments, dict) else {}
    return (
        "<tool_call>\n"
        f"<name>{html.escape(name or '')}</name>\n"
        "<parameters>\n"
        f"{_json_dumps_compact(args)}\n"
        "</parameters>\n"
        "</tool_call>"
    )


def format_tool_call_xml(name: str, arguments: Any) -> str:
    return _format_tool_call_xml(name, arguments)


def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def _format_observation_xml(tool_use_id: str, content: str, is_error: bool = False) -> str:
    return (
        "<observation>\n"
        f"{_cdata(content)}\n"
        "</observation>"
    )


def format_observation_xml(content: str, is_error: bool = False) -> str:
    return _format_observation_xml("", content, is_error=is_error)


def _convert_tool_messages_xml(role: str, content: list[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []

    for raw_block in content:
        block = _as_dict(raw_block)
        btype = block.get("type")

        if btype == "text":
            text_parts.append(block.get("text", "") or "")
        elif btype == "tool_use":
            text_parts.append(
                _format_tool_call_xml(block.get("name", ""), block.get("input", {}) or {})
            )
        elif btype == "tool_result":
            result_text = _extract_text(block.get("content", ""))
            is_error = bool(block.get("is_error", False))
            if is_error and not result_text.startswith("ERROR:"):
                result_text = f"ERROR: {result_text}"
            text_parts.append(
                _format_observation_xml(block.get("tool_use_id", ""), result_text, is_error)
            )
        elif btype == "image":
            text_parts.append("[image omitted]")

    return {"role": role, "content": "\n\n".join(p for p in text_parts if p)}


def _convert_tool_messages_native(
    role: str, content: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if role == "user":
        return _convert_native_user_message(content)
    if role == "assistant":
        return [_convert_native_assistant_message(content)]
    return [{"role": role, "content": _extract_text(content)}]


def _convert_native_user_message(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    text_parts: list[str] = []

    for raw_block in content:
        block = _as_dict(raw_block)
        btype = block.get("type")

        if btype == "tool_result":
            result_text = _extract_text(block.get("content", ""))
            if block.get("is_error", False) and not result_text.startswith("ERROR:"):
                result_text = f"ERROR: {result_text}"
            result.append({
                "role": "tool",
                "tool_call_id": block.get("tool_use_id", ""),
                "content": result_text,
            })
        elif btype == "text":
            text_parts.append(block.get("text", "") or "")
        elif btype == "image":
            text_parts.append("[image omitted]")

    text = "\n".join(p for p in text_parts if p)
    if text or not result:
        result.append({"role": "user", "content": text})
    return result


def _convert_native_assistant_message(content: list[dict[str, Any]]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for raw_block in content:
        block = _as_dict(raw_block)
        btype = block.get("type")

        if btype == "text":
            text_parts.append(block.get("text", "") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })

    return _build_assistant_msg(text_parts, tool_calls)


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
# XML tool call parser (CDATA-aware, position-based)
# ---------------------------------------------------------------
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

_TAG_TOOL_CALL_OPEN = "<tool_call"
_TAG_TOOL_CALL_CLOSE = "</tool_call>"
_TAG_NAME_OPEN = "<name>"
_TAG_NAME_CLOSE = "</name>"
_TAG_PARAMETERS_OPEN = "<parameters>"
_TAG_PARAMETERS_CLOSE = "</parameters>"
_TOOL_CALL_RE = re.compile(
    r"<tool_call(?:\s[^>]*)?>\s*<name>\s*(.*?)\s*</name>\s*"
    r"<parameters>\s*(.*?)\s*</parameters>\s*</tool_call>",
    re.DOTALL,
)
_PARTIAL_TOOL_CALL_RE = re.compile(
    r"<tool_call(?:\s[^>]*)?>\s*<name>\s*(.*?)\s*</name>\s*"
    r"<parameters>\s*(.*)\s*$",
    re.DOTALL,
)


def _find_outside_cdata(text: str, needle: str, start: int = 0) -> int:
    pos = start
    while True:
        idx = text.find(needle, pos)
        if idx == -1:
            return -1
        cdata_start = text.rfind(_CDATA_START, 0, idx)
        if cdata_start != -1:
            cdata_end = text.find(_CDATA_END, cdata_start)
            if cdata_end != -1 and idx < cdata_end:
                pos = cdata_end + len(_CDATA_END)
                continue
        return idx


def _rfind_outside_cdata(text: str, needle: str, start: int = 0, end: int | None = None) -> int:
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
                pos = cdata_start
                continue
        return idx


def _extract_cdata_or_text(raw: str) -> str:
    stripped = raw.strip()
    if stripped.startswith(_CDATA_START) and stripped.endswith(_CDATA_END):
        return stripped[len(_CDATA_START):-len(_CDATA_END)]
    return html.unescape(stripped)


def _coerce_param_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _parse_parameter_elements(body: str) -> dict[str, Any]:
    params: dict[str, Any] = {}
    pos = 0
    while True:
        tag_start = body.find(_TAG_PARAM_OPEN, pos)
        if tag_start == -1:
            break
        tag_end = body.find(">", tag_start)
        if tag_end == -1:
            break
        name_m = _PARAM_NAME_RE.search(body, tag_start, tag_end)
        if not name_m:
            pos = tag_end + 1
            continue
        p_name = name_m.group(1)
        close_start = _find_outside_cdata(body, _TAG_PARAM_CLOSE, tag_end + 1)
        if close_start == -1:
            break
        raw_value = body[tag_end + 1:close_start]
        params[p_name] = _coerce_param_value(_extract_cdata_or_text(raw_value))
        pos = close_start + len(_TAG_PARAM_CLOSE)
    return params


def _parse_invoke_elements(func_body: str, results: list[dict[str, Any]]) -> None:
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


def _parse_tool_call_block(raw_block: str, allow_partial: bool = False) -> dict[str, Any] | None:
    match = _TOOL_CALL_RE.search(raw_block)
    if not match and allow_partial:
        match = _PARTIAL_TOOL_CALL_RE.search(raw_block)
    if not match:
        return None

    name = html.unescape(match.group(1).strip())
    raw_params_body = match.group(2)
    if allow_partial:
        params_close = _find_outside_cdata(raw_params_body, _TAG_PARAMETERS_CLOSE)
        if params_close != -1:
            raw_params_body = raw_params_body[:params_close]
    raw_params = _extract_cdata_or_text(raw_params_body)
    try:
        parsed = json.loads(raw_params)
    except (json.JSONDecodeError, TypeError, ValueError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {"value": parsed}

    if not name:
        return None

    return {
        "id": f"toolu_{uuid.uuid4().hex[:24]}",
        "name": name,
        "input": parsed,
    }


def _parse_function_call_spans(text: str) -> list[tuple[int, int, list[dict[str, Any]]]]:
    spans: list[tuple[int, int, list[dict[str, Any]]]] = []
    pos = 0
    while True:
        start = _find_outside_cdata(text, _TAG_FUNC_OPEN, pos)
        if start == -1:
            break
        content_start = start + len(_TAG_FUNC_OPEN)
        end = _find_outside_cdata(text, _TAG_FUNC_CLOSE, content_start)
        if end == -1:
            break
        block_end = end + len(_TAG_FUNC_CLOSE)
        tool_blocks: list[dict[str, Any]] = []
        _parse_invoke_elements(text[content_start:end], tool_blocks)
        if tool_blocks:
            spans.append((start, block_end, tool_blocks))
        pos = block_end
    return spans


def _parse_tool_call_spans(text: str) -> list[tuple[int, int, list[dict[str, Any]]]]:
    spans: list[tuple[int, int, list[dict[str, Any]]]] = []
    pos = 0
    while True:
        start = _find_outside_cdata(text, _TAG_TOOL_CALL_OPEN, pos)
        if start == -1:
            break
        tag_end = text.find(">", start)
        if tag_end == -1:
            break
        close_start = _find_outside_cdata(text, _TAG_TOOL_CALL_CLOSE, tag_end + 1)
        if close_start == -1:
            tool = _parse_tool_call_block(text[start:], allow_partial=True)
            if tool:
                spans.append((start, len(text), [tool]))
            break
        block_end = close_start + len(_TAG_TOOL_CALL_CLOSE)
        tool = _parse_tool_call_block(text[start:block_end])
        if tool:
            spans.append((start, block_end, [tool]))
        pos = block_end
    return spans


def _xml_tool_call_spans(text: str) -> list[tuple[int, int, list[dict[str, Any]]]]:
    spans = _parse_function_call_spans(text) + _parse_tool_call_spans(text)
    spans.sort(key=lambda item: item[0])

    filtered: list[tuple[int, int, list[dict[str, Any]]]] = []
    cursor = -1
    for start, end, tools in spans:
        if start < cursor:
            continue
        filtered.append((start, end, tools))
        cursor = end
    return filtered


def find_xml_tool_call_start(text: str) -> int:
    candidates = [
        _find_outside_cdata(text, _TAG_FUNC_OPEN),
        _find_outside_cdata(text, _TAG_TOOL_CALL_OPEN),
    ]
    candidates = [idx for idx in candidates if idx != -1]
    return min(candidates) if candidates else -1


def find_xml_tool_call_end(text: str) -> int:
    ends: list[int] = []
    func_close = _rfind_outside_cdata(text, _TAG_FUNC_CLOSE)
    if func_close != -1:
        ends.append(func_close + len(_TAG_FUNC_CLOSE))
    tool_close = _rfind_outside_cdata(text, _TAG_TOOL_CALL_CLOSE)
    if tool_close != -1:
        ends.append(tool_close + len(_TAG_TOOL_CALL_CLOSE))
    return max(ends) if ends else -1


def find_xml_tool_call_consumed_end(text: str) -> int:
    spans = _xml_tool_call_spans(text)
    if not spans:
        return -1
    return max(end for _, end, _ in spans)


def has_xml_tool_call_marker(text: str) -> bool:
    return find_xml_tool_call_start(text) != -1


def parse_xml_tool_calls(text: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for _, _, tools in _xml_tool_call_spans(text):
        results.extend(tools)
    return results


def split_text_and_xml_tool_calls(text: str) -> list[dict[str, Any]]:
    spans = _xml_tool_call_spans(text)
    if not spans:
        return [{"type": "text", "text": text}] if text else []

    blocks: list[dict[str, Any]] = []
    cursor = 0
    for start, end, tools in spans:
        pre_text = text[cursor:start].strip()
        if pre_text:
            blocks.append({"type": "text", "text": pre_text})
        for tool in tools:
            block = dict(tool)
            block["type"] = "tool_use"
            blocks.append(block)
        cursor = end

    post_text = text[cursor:].strip()
    if post_text:
        blocks.append({"type": "text", "text": post_text})
    return blocks


# ---------------------------------------------------------------
# OpenAI tool_calls -> Anthropic content blocks
# ---------------------------------------------------------------
def normalize_tool_use_id(raw_id: Any = "") -> str:
    raw = str(raw_id or "")
    if not raw:
        return f"toolu_{uuid.uuid4().hex[:24]}"
    if raw.startswith("toolu_"):
        return raw
    if raw.startswith("call_"):
        return f"toolu_{raw[5:]}"
    if raw.startswith("fc_"):
        return f"toolu_{raw[3:]}"
    return f"toolu_{raw}"


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
            "id": normalize_tool_use_id(tc.get("id")),
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
    content_text = strip_model_thinking(flowith_result.get("content", "") or "")
    reasoning_text = flowith_result.get("reasoning_content", "") or ""
    tool_calls = flowith_result.get("tool_calls") or []
    usage = flowith_result.get("usage", {}) or {}
    finish_reason = flowith_result.get("finish_reason") or ""
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    content: list[dict[str, Any]] = []
    if reasoning_text:
        content.append({"type": "thinking", "thinking": reasoning_text})

    has_xml_tool_calls = has_xml_tool_call_marker(content_text)

    if has_xml_tool_calls:
        content.extend(split_text_and_xml_tool_calls(content_text))
    elif content_text:
        content.append({"type": "text", "text": content_text})

    if tool_calls:
        content.extend(openai_tool_calls_to_anthropic(tool_calls))

    if not content:
        content.append({"type": "text", "text": ""})

    has_any_tool_use = bool(tool_calls) or any(
        b.get("type") == "tool_use" for b in content
    )
    if has_any_tool_use:
        stop_reason = "tool_use"
    elif finish_reason == "length":
        stop_reason = "max_tokens"
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
# SSE event builders
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
