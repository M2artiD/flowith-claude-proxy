import os
import unittest

from fastapi.testclient import TestClient

from proxy import server
from proxy.adapter import (
    claude_request_to_flowith_messages,
    flowith_result_to_claude_response,
    parse_xml_tool_calls,
)


class FakeFlowithClient:
    def __init__(self) -> None:
        self.calls = []
        self.next_result = None

    def call_api(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.next_result is not None:
            return self.next_result
        return {
            "success": True,
            "content": "ok",
            "usage": {},
            "finish_reason": "stop",
        }


class ToolBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_server_api_key = server._SERVER_API_KEY
        self.original_default_client = server._default_client
        self.original_tool_mode = server.FLOWITH_TOOL_MODE
        self.original_profile = os.environ.get("FLOWITH_API_PROFILE")

        self.fake_client = FakeFlowithClient()
        server._SERVER_API_KEY = "test-key"
        server._default_client = self.fake_client
        server.FLOWITH_TOOL_MODE = "xml"
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server._SERVER_API_KEY = self.original_server_api_key
        server._default_client = self.original_default_client
        server.FLOWITH_TOOL_MODE = self.original_tool_mode
        if self.original_profile is None:
            os.environ.pop("FLOWITH_API_PROFILE", None)
        else:
            os.environ["FLOWITH_API_PROFILE"] = self.original_profile

    def test_codex_profile_disables_anthropic_messages(self) -> None:
        os.environ["FLOWITH_API_PROFILE"] = "codex"

        response = self.client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-4.6-sonnet",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_xml_tool_requests_add_tool_call_stop_sequence(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "stop_sequences": ["USER_STOP"],
                "tools": [
                    {
                        "name": "Bash",
                        "description": "Run a shell command",
                        "input_schema": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ],
                "messages": [{"role": "user", "content": "list files"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake_client.calls), 1)
        upstream_call = self.fake_client.calls[0]
        self.assertIsNone(upstream_call["tools"])
        self.assertIn("USER_STOP", upstream_call["stop_sequences"])
        self.assertIn("</tool_call>", upstream_call["stop_sequences"])
        self.assertIn("<tool_call>", upstream_call["messages"][0]["content"])

    def test_string_stop_sequence_is_preserved_with_tool_call_stop(self) -> None:
        response = self.client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "stop_sequences": "USER_STOP",
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "list files"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        stop_sequences = self.fake_client.calls[0]["stop_sequences"]
        self.assertEqual(stop_sequences, ["USER_STOP", "</tool_call>"])

    def test_non_streaming_xml_tool_response_becomes_tool_use(self) -> None:
        self.fake_client.next_result = {
            "success": True,
            "content": (
                "<tool_call>\n"
                "<name>Bash</name>\n"
                "<parameters>\n"
                '{"command":"pwd"}\n'
                "</parameters>\n"
                "</tool_call>"
            ),
            "usage": {},
            "finish_reason": "stop",
        }

        response = self.client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
                "messages": [{"role": "user", "content": "print cwd"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["stop_reason"], "tool_use")
        self.assertEqual(body["content"][0]["type"], "tool_use")
        self.assertEqual(body["content"][0]["name"], "Bash")
        self.assertEqual(body["content"][0]["input"], {"command": "pwd"})

    def test_tool_result_history_uses_plain_observation_xml(self) -> None:
        messages = claude_request_to_flowith_messages(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_123",
                                "content": "file list",
                            }
                        ],
                    }
                ]
            },
            anthropic_tools=[{"name": "Bash", "input_schema": {"type": "object"}}],
            tool_mode="xml",
        )

        self.assertEqual(messages[-1]["content"], "<observation>\n<![CDATA[file list]]>\n</observation>")

    def test_react_prompt_includes_uniclaude_style_example(self) -> None:
        messages = claude_request_to_flowith_messages(
            {
                "messages": [{"role": "user", "content": "list files"}],
            },
            anthropic_tools=[
                {
                    "name": "Bash",
                    "description": "Run a shell command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                }
            ],
            tool_mode="xml",
        )

        prompt = messages[0]["content"]
        self.assertIn("EXAMPLE", prompt)
        self.assertIn('"command": "ls -la"', prompt)
        self.assertIn("Output ONLY ONE tool call per response.", prompt)
        self.assertIn("STOP writing immediately after </tool_call>", prompt)

    def test_non_streaming_xml_tool_response_strips_think_blocks(self) -> None:
        response = flowith_result_to_claude_response(
            {
                "success": True,
                "content": (
                    "<think>I should call bash</think>\n"
                    "<tool_call>\n"
                    "<name>Bash</name>\n"
                    "<parameters>\n"
                    '{"command":"ls"}\n'
                    "</parameters>\n"
                    "</tool_call>"
                ),
                "usage": {},
                "finish_reason": "stop",
            },
            "claude-3-5-sonnet-20241022",
        )

        self.assertEqual(response["stop_reason"], "tool_use")
        self.assertEqual(response["content"], [
            {
                "type": "tool_use",
                "id": response["content"][0]["id"],
                "name": "Bash",
                "input": {"command": "ls"},
            }
        ])

    def test_parser_keeps_arguments_when_tool_call_stop_truncates_closing_tag(self) -> None:
        tool_calls = parse_xml_tool_calls(
            '<tool_call>\n'
            '<name>Bash</name>\n'
            '<parameters>\n'
            '{"command":"ls"}\n'
            '</parameters>\n'
        )

        self.assertEqual(tool_calls[0]["name"], "Bash")
        self.assertEqual(tool_calls[0]["input"], {"command": "ls"})

    def test_streaming_partial_tool_call_emits_only_tool_use(self) -> None:
        class StreamingFakeFlowithClient:
            def call_api(self, messages, **kwargs):
                kwargs["on_chunk"](
                    '<tool_call>\n'
                    '<name>Bash</name>\n'
                    '<parameters>\n'
                    '{"command":"ls"}\n'
                    '</parameters>\n'
                )
                return {
                    "success": True,
                    "content": "",
                    "usage": {},
                    "finish_reason": "stop",
                }

        events = b"".join(
            server._stream_claude_events(
                StreamingFakeFlowithClient(),
                messages=[{"role": "user", "content": "list files"}],
                requested_model="claude-3-5-sonnet-20241022",
                has_tools=True,
            )
        ).decode("utf-8")

        self.assertIn('"type": "tool_use"', events)
        self.assertIn('"name": "Bash"', events)
        self.assertIn('"type": "input_json_delta"', events)
        self.assertIn('{\\"command\\": \\"ls\\"}', events)
        self.assertNotIn("<tool_call>", events)
        self.assertIn('"stop_reason": "tool_use"', events)

    def test_streaming_tool_response_strips_think_blocks(self) -> None:
        class StreamingFakeFlowithClient:
            def call_api(self, messages, **kwargs):
                kwargs["on_chunk"]("<think>I should inspect files</think>\n<tool")
                kwargs["on_chunk"](
                    '_call>\n'
                    '<name>Bash</name>\n'
                    '<parameters>\n'
                    '{"command":"ls"}\n'
                    '</parameters>\n'
                )
                return {
                    "success": True,
                    "content": "",
                    "usage": {},
                    "finish_reason": "stop",
                }

        events = b"".join(
            server._stream_claude_events(
                StreamingFakeFlowithClient(),
                messages=[{"role": "user", "content": "list files"}],
                requested_model="claude-3-5-sonnet-20241022",
                has_tools=True,
            )
        ).decode("utf-8")

        self.assertIn('"type": "tool_use"', events)
        self.assertNotIn("<think>", events)
        self.assertNotIn("I should inspect files", events)
        self.assertNotIn("<tool_call>", events)
        self.assertIn('"stop_reason": "tool_use"', events)

    def test_streaming_with_tools_preserves_plain_text_before_and_after_xml_tool_call(self) -> None:
        class StreamingFakeFlowithClient:
            def call_api(self, messages, **kwargs):
                kwargs["on_chunk"]("Need cwd. ")
                kwargs["on_chunk"](
                    '<tool_call>\n'
                    '<name>Bash</name>\n'
                    '<parameters>\n'
                    '{"command":"pwd"}\n'
                    '</parameters>\n'
                    '</tool_call>'
                )
                kwargs["on_chunk"](" Done.")
                return {
                    "success": True,
                    "content": "",
                    "usage": {},
                    "finish_reason": "stop",
                }

        events = b"".join(
            server._stream_claude_events(
                StreamingFakeFlowithClient(),
                messages=[{"role": "user", "content": "print cwd"}],
                requested_model="claude-3-5-sonnet-20241022",
                has_tools=True,
            )
        ).decode("utf-8")

        self.assertIn("Need cwd. ", events)
        self.assertIn(" Done.", events)
        self.assertIn('"type": "tool_use"', events)
        self.assertIn('"name": "Bash"', events)
        self.assertNotIn("<tool_call>", events)


if __name__ == "__main__":
    unittest.main()
