import json
import os
from contextlib import redirect_stdout
from io import StringIO
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
        self.original_request_log = server.FLOWITH_REQUEST_LOG
        self.original_profile = os.environ.get("FLOWITH_API_PROFILE")

        self.fake_client = FakeFlowithClient()
        server._SERVER_API_KEY = "test-key"
        server._default_client = self.fake_client
        server.FLOWITH_TOOL_MODE = "xml"
        server.FLOWITH_REQUEST_LOG = False
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server._SERVER_API_KEY = self.original_server_api_key
        server._default_client = self.original_default_client
        server.FLOWITH_TOOL_MODE = self.original_tool_mode
        server.FLOWITH_REQUEST_LOG = self.original_request_log
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
                "model": "claude-5-sonnet",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_local_only_host_check_accepts_loopback_and_rejects_remote(self) -> None:
        self.assertTrue(server._is_local_client_host("127.0.0.1"))
        self.assertTrue(server._is_local_client_host("::1"))
        self.assertTrue(server._is_local_client_host("localhost"))
        self.assertTrue(server._is_local_client_host("testclient"))
        self.assertFalse(server._is_local_client_host("203.0.113.10"))
        self.assertFalse(server._is_local_client_host("example.com"))

    def test_request_log_is_disabled_by_default(self) -> None:
        capture = StringIO()
        with redirect_stdout(capture):
            response = self.client.post(
                "/v1/messages",
                headers={"x-api-key": "test-key"},
                json={
                    "model": "claude-5-sonnet",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hello"}],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("[REQ]", capture.getvalue())

    def test_xml_tool_requests_do_not_inject_tool_call_stop_sequence(self) -> None:
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
        self.assertNotIn("</tool_call>", upstream_call["stop_sequences"])
        self.assertIn("<tool_call>", upstream_call["messages"][0]["content"])

    def test_string_stop_sequence_is_preserved_without_tool_call_stop(self) -> None:
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
        self.assertEqual(stop_sequences, ["USER_STOP"])

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

    def test_goal_stop_hook_plain_text_response_becomes_strict_json(self) -> None:
        self.fake_client.next_result = {
            "success": True,
            "content": "Goal is completed. The required context has been read and work has started.",
            "usage": {},
            "finish_reason": "stop",
        }

        response = self.client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "system": (
                    "You are a Stop hook evaluator for /goal. "
                    "Respond with JSON: {\"ok\": true} or "
                    "{\"ok\": false, \"reason\": \"why\"}."
                ),
                "messages": [
                    {
                        "role": "user",
                        "content": "Condition: Read the context and execute the task. Decide whether the goal is met.",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["content"][0]["text"]
        parsed = json.loads(text)
        self.assertTrue(parsed["ok"])
        self.assertIn("Goal is completed", parsed["reason"])

    def test_streaming_goal_stop_hook_plain_text_response_becomes_strict_json(self) -> None:
        self.fake_client.next_result = {
            "success": True,
            "content": "Goal is completed. The required context has been read and work has started.",
            "usage": {},
            "finish_reason": "stop",
        }

        response = self.client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "stream": True,
                "system": (
                    "You are evaluating a stop-condition hook in Claude Code. "
                    "Your response must be a JSON object with one of these shapes: "
                    "{\"ok\": true, \"reason\": \"evidence\"} or "
                    "{\"ok\": false, \"reason\": \"missing\"}."
                ),
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "A session-scoped Stop hook is now active with condition: "
                            "\"Read the context and execute the task\". Decide whether the goal is met."
                        ),
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        deltas = []
        for raw_line in response.text.splitlines():
            if not raw_line.startswith("data: "):
                continue
            event = json.loads(raw_line[6:])
            if event.get("type") != "content_block_delta":
                continue
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                deltas.append(delta.get("text", ""))
        parsed = json.loads("".join(deltas))
        self.assertTrue(parsed["ok"])
        self.assertIn("Goal is completed", parsed["reason"])
        self.assertFalse(self.fake_client.calls[0]["stream"])

    def test_plain_goal_chat_is_not_misclassified_as_hook_json(self) -> None:
        self.fake_client.next_result = {
            "success": True,
            "content": "??????????????????",
            "usage": {},
            "finish_reason": "stop",
        }

        response = self.client.post(
            "/v1/messages",
            headers={"x-api-key": "test-key"},
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 256,
                "system": "????????? hook ???",
                "messages": [
                    {
                        "role": "user",
                        "content": "/goal ????????????? stop hook error: JSON validation failed?",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        text = response.json()["content"][0]["text"]
        self.assertEqual(text, "??????????????????")

    def test_goal_keywords_without_explicit_json_contract_do_not_trigger_hook_mode(self) -> None:
        body = {
            "system": "You are a helpful assistant discussing /goal behavior.",
            "messages": [],
        }
        messages = [
            {
                "role": "user",
                "content": "A stop hook mentioned a goal condition, but this is not asking for JSON.",
            }
        ]

        self.assertFalse(server._looks_like_hook_json_request(body, messages))

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
        self.assertIn("Use a tool only when it is needed", prompt)
        self.assertIn("If the user request can be answered directly", prompt)
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

    def test_parser_recovers_split_escaped_cdata_parameters(self) -> None:
        # Regression: model wraps JSON params in CDATA and split-escapes the
        # CDATA terminator (mirroring the write-side escape for tool results).
        # The reader must concatenate consecutive CDATA sections; previously
        # the split junk stayed inside the JSON, json.loads failed, and the
        # tool input silently degraded to {} ("Invalid tool parameters" in
        # Claude Code). Tokens are built programmatically so this file never
        # contains a literal CDATA terminator.
        cdata_start = "<![CDATA" + "["
        cdata_end = "]" * 2 + ">"
        script = "echo hi\n# contains " + cdata_end + " inside"
        payload = json.dumps({"script": script})
        wrapped = (
            cdata_start
            + payload.replace(cdata_end, "]]" + cdata_end + cdata_start + ">")
            + cdata_end
        )

        tool_calls = parse_xml_tool_calls(
            "<tool_call>\n"
            "<name>Bash</name>\n"
            "<parameters>\n"
            + wrapped
            + "\n</parameters>\n"
            "</tool_call>"
        )

        self.assertEqual(tool_calls[0]["name"], "Bash")
        self.assertEqual(tool_calls[0]["input"], {"script": script})

    def test_parser_plain_cdata_parameters_unchanged(self) -> None:
        cdata_start = "<![CDATA" + "["
        cdata_end = "]" * 2 + ">"
        tool_calls = parse_xml_tool_calls(
            "<tool_call>\n"
            "<name>Bash</name>\n"
            "<parameters>\n"
            + cdata_start
            + '{"command":"ls"}'
            + cdata_end
            + "\n</parameters>\n"
            "</tool_call>"
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

    def test_streaming_preserves_final_partial_think_prefix_as_text(self) -> None:
        class StreamingFakeFlowithClient:
            def call_api(self, messages, **kwargs):
                kwargs["on_chunk"]("answer <th")
                return {
                    "success": True,
                    "content": "answer <th",
                    "usage": {},
                    "finish_reason": "stop",
                }

        events = b"".join(
            server._stream_claude_events(
                StreamingFakeFlowithClient(),
                messages=[{"role": "user", "content": "echo final tail"}],
                requested_model="claude-3-5-sonnet-20241022",
                has_tools=False,
            )
        ).decode("utf-8")

        self.assertIn("answer ", events)
        self.assertIn("<th", events)
        self.assertIn('"stop_reason": "end_turn"', events)

    def test_streaming_empty_upstream_result_finishes_without_api_error(self) -> None:
        class EmptyStreamingFakeFlowithClient:
            def call_api(self, messages, **kwargs):
                return {
                    "success": False,
                    "error": "Upstream stream ended without content",
                }

        events = b"".join(
            server._stream_claude_events(
                EmptyStreamingFakeFlowithClient(),
                messages=[{"role": "user", "content": "hello"}],
                requested_model="claude-3-5-sonnet-20241022",
                has_tools=False,
            )
        ).decode("utf-8")

        self.assertNotIn('"type": "error"', events)
        self.assertNotIn("API Error", events)
        self.assertNotIn("Upstream stream ended without content", events)
        self.assertIn('"stop_reason": "end_turn"', events)
        self.assertIn('"type": "message_stop"', events)

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
