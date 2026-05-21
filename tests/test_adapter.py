"""Unit tests for flowith_claude_proxy.adapter — pure function tests."""

import json
import pytest

from flowith_claude_proxy.adapter import (
    MODEL_ALIASES,
    _extract_text,
    claude_request_to_flowith_messages,
    flowith_result_to_claude_response,
    map_model,
    new_message_id,
    sse_content_block_delta,
    sse_content_block_start,
    sse_content_block_stop,
    sse_error,
    sse_message_delta,
    sse_message_start,
    sse_message_stop,
    sse_ping,
)


# ── map_model ──────────────────────────────────────────────────

class TestMapModel:
    def test_known_alias(self):
        assert map_model("claude-3-5-sonnet-20241022") == "claude-4.6-sonnet"

    def test_opus_alias(self):
        assert map_model("claude-opus-4-20250514") == "claude-opus-4.7"

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

    def test_unrecognized_non_claude(self):
        assert map_model("llama-3") == "claude-4.6-sonnet"

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


# ── flowith_result_to_claude_response ──────────────────────────

class TestResultConversion:
    def test_basic(self):
        result = flowith_result_to_claude_response(
            {"content": "hi", "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            "claude-3-5-sonnet-20241022",
        )
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["model"] == "claude-3-5-sonnet-20241022"
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
