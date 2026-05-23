"""Unit tests for flowith_claude_proxy.adapter — pure function tests."""

import json
import pytest

from flowith_claude_proxy.adapter import (
    _extract_text,
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
    sse_tool_input_delta,
)


# ── map_model ──────────────────────────────────────────────────

class TestMapModel:
    def test_passthrough_claude_sonnet(self):
        assert map_model("claude-4.6-sonnet") == "claude-4.6-sonnet"

    def test_passthrough_claude_opus(self):
        assert map_model("claude-opus-4.7") == "claude-opus-4.7"

    def test_passthrough_gpt(self):
        assert map_model("gpt-5.4") == "gpt-5.4"

    def test_passthrough_gemini(self):
        assert map_model("gemini-2.5-pro") == "gemini-2.5-pro"

    def test_passthrough_unknown_claude(self):
        assert map_model("claude-future-model") == "claude-future-model"

    def test_empty_returns_default(self):
        assert map_model("") == "claude-4.6-sonnet"

    def test_none_returns_default(self):
        assert map_model(None) == "claude-4.6-sonnet"

    def test_unrecognized_falls_back_to_default(self):
        assert map_model("randomstring") == "claude-4.6-sonnet"

    def test_custom_default(self):
        assert map_model("", default="gpt-5.4") == "gpt-5.4"


# ── _extract_text ──────────────────────────────────────────────

class TestExtractText:
    def test_string(self):
        assert _extract_text("hello") == "hello"

    def test_none(self):
        assert _extract_text(None) == ""

    def test_list_of_text_blocks(self):
        blocks = [
            {"type": "text", "text": "hello "},
            {"type": "text", "text": "world"},
        ]
        assert _extract_text(blocks) == "hello \nworld"

    def test_tool_use_block(self):
        blocks = [
            {"type": "tool_use", "name": "bash", "input": {"cmd": "ls"}},
        ]
        result = _extract_text(blocks)
        assert "tool_use" in result
        assert "bash" in result

    def test_tool_result_block(self):
        blocks = [
            {"type": "tool_result", "content": "file1.txt"},
        ]
        result = _extract_text(blocks)
        assert "file1.txt" in result

    def test_image_block_omitted(self):
        blocks = [
            {"type": "image", "source": {"data": "..."}},
        ]
        assert _extract_text(blocks) == "[image omitted]"

    def test_mixed_blocks(self):
        blocks = [
            {"type": "text", "text": "hi"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "there"},
        ]
        result = _extract_text(blocks)
        assert "hi" in result
        assert "[image omitted]" in result
        assert "there" in result

    def test_list_of_strings(self):
        assert _extract_text(["a", "b"]) == "a\nb"

    def test_fallback_str(self):
        assert _extract_text(42) == "42"


# ── anthropic_tools_to_openai ──────────────────────────────────

class TestAnthropicToolsToOpenAI:
    def test_basic_conversion(self):
        tools = [
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            }
        ]
        result = anthropic_tools_to_openai(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "get_weather"
        assert result[0]["function"]["parameters"]["properties"]["city"]["type"] == "string"

    def test_computer_tool_skipped(self):
        tools = [
            {"type": "computer_20250124", "name": "computer"},
            {"name": "bash", "description": "Run bash", "input_schema": {"type": "object", "properties": {}}},
        ]
        result = anthropic_tools_to_openai(tools)
        assert len(result) == 1
        assert result[0]["function"]["name"] == "bash"

    def test_empty_list(self):
        assert anthropic_tools_to_openai([]) == []


# ── anthropic_tool_choice_to_openai ────────────────────────────

class TestAnthropicToolChoiceToOpenAI:
    def test_auto(self):
        assert anthropic_tool_choice_to_openai("auto") == "auto"

    def test_none(self):
        assert anthropic_tool_choice_to_openai(None) == "auto"

    def test_any(self):
        assert anthropic_tool_choice_to_openai("any") == "required"

    def test_specific_tool(self):
        result = anthropic_tool_choice_to_openai({"type": "tool", "name": "bash"})
        assert result == {"type": "function", "function": {"name": "bash"}}


# ── claude_request_to_flowith_messages ─────────────────────────

class TestRequestConversion:
    def test_simple_user_message(self):
        body = {
            "messages": [{"role": "user", "content": "hello"}],
        }
        msgs = claude_request_to_flowith_messages(body)
        assert len(msgs) == 1
        assert msgs[0] == {"role": "user", "content": "hello"}

    def test_system_prompt(self):
        body = {
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "hi"}],
        }
        msgs = claude_request_to_flowith_messages(body)
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_system_as_list(self):
        body = {
            "system": [{"type": "text", "text": "Be concise."}],
            "messages": [{"role": "user", "content": "ok"}],
        }
        msgs = claude_request_to_flowith_messages(body)
        assert msgs[0]["content"] == "Be concise."

    def test_invalid_role_becomes_user(self):
        body = {
            "messages": [{"role": "tool", "content": "output"}],
        }
        msgs = claude_request_to_flowith_messages(body)
        assert msgs[0]["role"] == "user"

    def test_empty_messages(self):
        assert claude_request_to_flowith_messages({"messages": []}) == []

    def test_no_messages_key(self):
        assert claude_request_to_flowith_messages({}) == []

    def test_assistant_tool_use_converted(self):
        body = {
            "messages": [
                {"role": "user", "content": "run ls"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_123", "name": "bash", "input": {"command": "ls"}},
                    ],
                },
            ],
        }
        msgs = claude_request_to_flowith_messages(body)
        assert len(msgs) == 2
        assert msgs[1]["role"] == "assistant"
        assert "tool_calls" in msgs[1]
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "bash"
        assert msgs[1]["tool_calls"][0]["id"] == "toolu_123"

    def test_tool_result_converted(self):
        body = {
            "messages": [
                {"role": "user", "content": "run ls"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_123", "name": "bash", "input": {"command": "ls"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_123", "content": "file1.txt\nfile2.txt"},
                    ],
                },
            ],
        }
        msgs = claude_request_to_flowith_messages(body)
        # tool_result → user message with XML-style markers
        assert any("tool_result" in str(m.get("content", "")) for m in msgs)
        tool_msg = next(
            m for m in msgs
            if isinstance(m.get("content"), str) and "tool_result" in m["content"]
        )
        assert tool_msg["role"] == "user"
        assert 'id="toolu_123"' in tool_msg["content"]
        assert "file1.txt" in tool_msg["content"]
        assert "</tool_result>" in tool_msg["content"]

    def test_tool_result_with_error_flag(self):
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_err", "content": "Permission denied", "is_error": True},
                    ],
                },
            ],
        }
        msgs = claude_request_to_flowith_messages(body)
        tool_msg = next(
            m for m in msgs
            if isinstance(m.get("content"), str) and "tool_result" in m["content"]
        )
        assert "error" in tool_msg["content"]
        assert "Permission denied" in tool_msg["content"]

    def test_mixed_text_and_tool_use(self):
        body = {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "I'll run that."},
                        {"type": "tool_use", "id": "toolu_abc", "name": "bash", "input": {"command": "pwd"}},
                    ],
                },
            ],
        }
        msgs = claude_request_to_flowith_messages(body)
        assert len(msgs) == 1
        assert msgs[0]["role"] == "assistant"
        assert "tool_calls" in msgs[0]
        assert msgs[0]["content"] == "I'll run that."


# ── openai_tool_calls_to_anthropic ─────────────────────────────

class TestOpenAIToolCallsToAnthropic:
    def test_basic_conversion(self):
        tool_calls = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "bash",
                    "arguments": '{"command": "ls"}',
                },
            }
        ]
        result = openai_tool_calls_to_anthropic(tool_calls)
        assert len(result) == 1
        assert result[0]["type"] == "tool_use"
        assert result[0]["id"] == "call_abc123"
        assert result[0]["name"] == "bash"
        assert result[0]["input"] == {"command": "ls"}

    def test_invalid_arguments_json(self):
        tool_calls = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "bash", "arguments": "invalid{"},
            }
        ]
        result = openai_tool_calls_to_anthropic(tool_calls)
        assert result[0]["input"] == {}

    def test_empty_list(self):
        assert openai_tool_calls_to_anthropic([]) == []


# ── flowith_result_to_claude_response ──────────────────────────

class TestResultConversion:
    def test_basic(self):
        result = flowith_result_to_claude_response(
            {"content": "hi", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            "claude-4.6-sonnet",
        )
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["model"] == "claude-4.6-sonnet"
        assert result["content"][0]["text"] == "hi"
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5
        assert result["usage"]["cache_creation_input_tokens"] == 0
        assert result["usage"]["cache_read_input_tokens"] == 0

    def test_empty_content(self):
        result = flowith_result_to_claude_response({}, "gpt-5.4")
        assert result["content"][0]["text"] == ""
        assert result["usage"]["input_tokens"] == 0

    def test_message_id_format(self):
        mid = new_message_id()
        assert mid.startswith("msg_")
        assert len(mid) == 28  # "msg_" + 24 hex chars

    def test_tool_calls_in_response(self):
        result = flowith_result_to_claude_response(
            {
                "content": "",
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                    }
                ],
            },
            "claude-4.6-sonnet",
        )
        assert result["stop_reason"] == "tool_use"
        tool_block = next(b for b in result["content"] if b["type"] == "tool_use")
        assert tool_block["name"] == "bash"
        assert tool_block["input"] == {"command": "ls"}

    def test_text_and_tool_calls(self):
        result = flowith_result_to_claude_response(
            {
                "content": "Running that for you.",
                "usage": {"prompt_tokens": 5, "completion_tokens": 10},
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command":"ls"}'},
                    }
                ],
            },
            "claude-4.6-sonnet",
        )
        assert result["stop_reason"] == "tool_use"
        text_block = next(b for b in result["content"] if b["type"] == "text")
        assert text_block["text"] == "Running that for you."
        tool_block = next(b for b in result["content"] if b["type"] == "tool_use")
        assert tool_block["name"] == "bash"


# ── XML tool call parser (robust CDATA-aware) ───────────────────

class TestParseXmlToolCalls:
    def test_empty_string(self):
        assert parse_xml_tool_calls("") == []

    def test_no_xml(self):
        assert parse_xml_tool_calls("plain text") == []

    def test_simple_cdata_tool_call(self):
        xml = (
            "<function_calls>\n"
            '<invoke name="bash">\n'
            '<parameter name="command"><![CDATA[ls -la]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert len(results) == 1
        assert results[0]["name"] == "bash"
        assert results[0]["input"] == {"command": "ls -la"}

    def test_multiple_params(self):
        xml = (
            "<function_calls>\n"
            '<invoke name="write">\n'
            '<parameter name="file_path"><![CDATA[/tmp/test.txt]]></parameter>\n'
            '<parameter name="content"><![CDATA[hello world]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert len(results) == 1
        assert results[0]["input"]["file_path"] == "/tmp/test.txt"
        assert results[0]["input"]["content"] == "hello world"

    def test_json_param_value(self):
        xml = (
            "<function_calls>\n"
            '<invoke name="edit">\n'
            '<parameter name="filters"><![CDATA[{"pattern":"*.py","caseSensitive":false}]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert results[0]["input"]["filters"] == {"pattern": "*.py", "caseSensitive": False}

    def test_special_chars_shell_redirect(self):
        """Shell commands with <, >, | inside CDATA must parse correctly."""
        xml = (
            "<function_calls>\n"
            '<invoke name="bash">\n'
            '<parameter name="command"><![CDATA[cat file.txt | grep "error" > /tmp/errors.log]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert results[0]["input"]["command"] == 'cat file.txt | grep "error" > /tmp/errors.log'

    def test_special_chars_xml_like(self):
        """CDATA value containing text that looks like closing tags."""
        xml = (
            "<function_calls>\n"
            '<invoke name="bash">\n'
            '<parameter name="command"><![CDATA[echo "</function_calls></invoke></parameter>"]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert results[0]["input"]["command"] == 'echo "</function_calls></invoke></parameter>"'

    def test_special_chars_windows_paths(self):
        """Windows paths with backslashes inside CDATA."""
        xml = (
            "<function_calls>\n"
            '<invoke name="read">\n'
            '<parameter name="file_path"><![CDATA[C:\\Users\\test\\file.txt]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert results[0]["input"]["file_path"] == "C:\\Users\\test\\file.txt"

    def test_multiple_invokes_in_one_block(self):
        xml = (
            "<function_calls>\n"
            '<invoke name="bash">\n'
            '<parameter name="command"><![CDATA[pwd]]></parameter>\n'
            "</invoke>\n"
            '<invoke name="read">\n'
            '<parameter name="file_path"><![CDATA[/tmp/out.txt]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert len(results) == 2
        assert results[0]["name"] == "bash"
        assert results[1]["name"] == "read"

    def test_multiple_function_calls_blocks(self):
        xml = (
            "<function_calls>"
            '<invoke name="bash"><parameter name="command"><![CDATA[ls]]></parameter></invoke>'
            "</function_calls>"
            " some text "
            "<function_calls>"
            '<invoke name="read"><parameter name="file_path"><![CDATA[/tmp/x.txt]]></parameter></invoke>'
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert len(results) == 2
        assert results[0]["name"] == "bash"
        assert results[1]["name"] == "read"

    def test_number_coercion(self):
        xml = (
            "<function_calls>\n"
            '<invoke name="count">\n'
            '<parameter name="limit"><![CDATA[42]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert results[0]["input"]["limit"] == 42

    def test_bool_coercion(self):
        xml = (
            "<function_calls>\n"
            '<invoke name="toggle">\n'
            '<parameter name="verbose"><![CDATA[true]]></parameter>\n'
            '<parameter name="quiet"><![CDATA[false]]></parameter>\n'
            "</invoke>\n"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert results[0]["input"]["verbose"] is True
        assert results[0]["input"]["quiet"] is False

    def test_malformed_xml_recovers(self):
        """Malformed XML should not crash — just return no tool calls."""
        # Missing close tag
        xml = (
            "<function_calls>\n"
            '<invoke name="bash">\n'
            '<parameter name="command"><![CDATA[ls]]></parameter>\n'
            "</invoke>\n"
        )
        results = parse_xml_tool_calls(xml)
        # Should handle gracefully — no crash
        assert isinstance(results, list)

    def test_fallback_raw_text_no_cdata(self):
        """Raw text (no CDATA) with XML entities should still parse."""
        xml = (
            "<function_calls>"
            '<invoke name="bash">'
            '<parameter name="command">ls &amp;&amp; echo done</parameter>'
            "</invoke>"
            "</function_calls>"
        )
        results = parse_xml_tool_calls(xml)
        assert len(results) == 1
        assert results[0]["input"]["command"] == "ls && echo done"


class TestSplitTextAndXmlToolCalls:
    def test_text_before_tool_call(self):
        text = "Let me run that for you.\n<function_calls>\n<invoke name=\"bash\">\n<parameter name=\"command\"><![CDATA[ls]]></parameter>\n</invoke>\n</function_calls>"
        blocks = split_text_and_xml_tool_calls(text)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "text"
        assert "Let me run that" in blocks[0]["text"]
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "bash"

    def test_text_after_tool_call(self):
        text = "<function_calls>\n<invoke name=\"bash\">\n<parameter name=\"command\"><![CDATA[ls]]></parameter>\n</invoke>\n</function_calls>\nDone."
        blocks = split_text_and_xml_tool_calls(text)
        assert len(blocks) == 2
        assert blocks[0]["type"] == "tool_use"
        assert blocks[1]["type"] == "text"
        assert blocks[1]["text"] == "Done."

    def test_text_between_and_after(self):
        text = (
            "Before.\n"
            "<function_calls>"
            '<invoke name="bash"><parameter name="command"><![CDATA[ls]]></parameter></invoke>'
            "</function_calls>"
            "Between.\n"
            "<function_calls>"
            '<invoke name="read"><parameter name="file_path"><![CDATA[/tmp/x.txt]]></parameter></invoke>'
            "</function_calls>"
            "After."
        )
        blocks = split_text_and_xml_tool_calls(text)
        # Before | bash | Between | read | After = 5 blocks
        assert len(blocks) == 5
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "Before."
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["name"] == "bash"
        assert blocks[2]["type"] == "text"
        assert blocks[2]["text"] == "Between."
        assert blocks[3]["type"] == "tool_use"
        assert blocks[3]["name"] == "read"
        assert blocks[4]["type"] == "text"
        assert blocks[4]["text"] == "After."

    def test_no_xml_returns_single_text_block(self):
        blocks = split_text_and_xml_tool_calls("just text")
        assert len(blocks) == 1
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "just text"

    def test_empty_string(self):
        assert split_text_and_xml_tool_calls("") == []


# ── SSE helpers ────────────────────────────────────────────────

class TestSSE:
    def test_message_start(self):
        sse = sse_message_start("msg_abc", "claude-4.6-sonnet", input_tokens=5)
        assert "event: message_start" in sse
        assert '"id": "msg_abc"' in sse
        assert '"input_tokens": 5' in sse

    def test_content_block_delta(self):
        sse = sse_content_block_delta("hello", index=0)
        assert "event: content_block_delta" in sse
        assert '"text": "hello"' in sse

    def test_message_stop(self):
        sse = sse_message_stop()
        assert "event: message_stop" in sse

    def test_ping(self):
        sse = sse_ping()
        assert "event: ping" in sse

    def test_error(self):
        sse = sse_error("something broke")
        assert "event: error" in sse
        assert "something broke" in sse

    def test_tool_use_content_block_start(self):
        sse = sse_content_block_start(1, block_type="tool_use", tool_id="toolu_abc", tool_name="bash")
        assert "event: content_block_start" in sse
        assert '"type": "tool_use"' in sse
        assert '"id": "toolu_abc"' in sse
        assert '"name": "bash"' in sse

    def test_tool_input_delta(self):
        sse = sse_tool_input_delta('{"command": "ls"}', index=1)
        assert "event: content_block_delta" in sse
        assert '"type": "input_json_delta"' in sse
        assert '"partial_json"' in sse
