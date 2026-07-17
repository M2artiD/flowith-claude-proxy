import json
import os
from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from proxy import server
from proxy.adapter import (
    build_tool_xml_prompt,
    claude_request_to_flowith_messages,
    find_xml_tool_call_start,
    flowith_result_to_claude_response,
    has_xml_tool_call_marker,
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
    def test_parse_tool_call_repairs_singular_parameters_close(self) -> None:
        text = (
            "<tool_call>\n"
            "<name>shell_command</name>\n"
            "<parameters>\n"
            '{"command":"Get-Location"}\n'
            "</parameter>\n"
            "</tool_call>"
        )

        tools = parse_xml_tool_calls(text)

        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "shell_command")
        self.assertEqual(tools[0]["input"], {"command": "Get-Location"})

    def test_tool_prompt_requires_explicitly_requested_actions(self) -> None:
        prompt = build_tool_xml_prompt([
            {
                "name": "shell_command",
                "description": "Run a shell command.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ])

        self.assertIn("explicitly asks you to use a tool", prompt)
        self.assertIn("Do not claim that you cannot access or execute", prompt)
        self.assertIn("tool observation reports the failure.", prompt)
        self.assertIn("update_plan", prompt)
        self.assertTrue(prompt.rstrip().endswith("rather than long prose."))
        example_prefix = prompt.split("<tool_call>", 2)[1]
        self.assertNotIn("I need to", example_prefix)

    def test_tool_prompt_forbids_avoiding_visible_app_actions(self) -> None:
        prompt = build_tool_xml_prompt([
            {
                "name": "shell_command",
                "description": "Run a shell command.",
                "input_schema": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            }
        ])

        self.assertIn("CMD, PowerShell, a terminal, browser, window, GUI application, or process", prompt)
        self.assertIn("launch it visibly", prompt)
        self.assertIn("Do not evade tool use", prompt)
        self.assertIn("tell the user to perform the action manually", prompt)
        self.assertIn("Creating or writing a file at a user-requested path", prompt)
        self.assertIn("Never say that an action is starting, in progress, or complete", prompt)
        self.assertIn("until an <observation> confirms it", prompt)
        self.assertIn("Never promise to call a tool later", prompt)
        self.assertIn("one concise user-visible action note", prompt)
        self.assertIn("exact command", prompt)
        self.assertIn("not hidden chain-of-thought", prompt)
        self.assertIn("must not include a result, success/failure claim, or final answer", prompt)
        self.assertIn("greetings, casual conversation, or explanation-only questions", prompt)

    def test_tool_prompt_caps_large_top_level_descriptions(self) -> None:
        description = "tool details " * 2_000

        prompt = build_tool_xml_prompt([
            {
                "name": "large_tool",
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            }
        ])

        self.assertIn(description[:300], prompt)
        self.assertNotIn(description[:301], prompt)
        self.assertLess(len(prompt), 5_000)

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
        self.assertIn("You may emit multiple sequential <tool_call> blocks", prompt)
        self.assertIn("update_plan", prompt)
        self.assertIn("STOP writing immediately after the last </tool_call>", prompt)

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

    def test_prose_mentioning_tag_names_in_backticks_is_not_a_tool_call(self) -> None:
        text = (
            "No raw XML tags (`<tool_call>`, `<function_calls>`, "
            "`<parameter>`, CDATA markers) leaked into visible text output."
        )

        self.assertFalse(has_xml_tool_call_marker(text))
        self.assertEqual(find_xml_tool_call_start(text), -1)
        self.assertEqual(parse_xml_tool_calls(text), [])

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

    def test_streaming_exhausted_empty_response_finishes_with_retry_message(self) -> None:
        class EmptyStreamingFakeFlowithClient:
            def call_api(self, messages, **kwargs):
                return {
                    "success": False,
                    "empty_response": True,
                    "error": "Upstream returned no content after the bounded retry budget.",
                }

        events = b"".join(
            server._stream_claude_events(
                EmptyStreamingFakeFlowithClient(),
                messages=[{"role": "user", "content": "use a tool"}],
                requested_model="claude-fable-5",
                has_tools=True,
            )
        ).decode("utf-8")

        self.assertNotIn('"type": "error"', events)
        self.assertIn("upstream returned no content", events)
        self.assertIn('"stop_reason": "end_turn"', events)
        self.assertIn('"type": "message_stop"', events)

    def test_fable_exhausted_empty_response_uses_non_fable_model_fallback(self) -> None:
        class ModelFallbackClient:
            model = "claude-fable-5"

            def __init__(self) -> None:
                self.calls = []

            def call_api(self, messages, **kwargs):
                self.calls.append({"messages": messages, "model": kwargs.get("model")})
                if kwargs.get("model") == "claude-fable-5":
                    return {
                        "success": False,
                        "empty_response": True,
                        "error": "Upstream returned no content after the bounded retry budget.",
                    }
                kwargs["on_chunk"](
                    '<tool_call>\n<name>Bash</name>\n'
                    '<parameters>{"command":"pwd"}</parameters>\n</tool_call>'
                )
                return {"success": True, "content": "", "usage": {}, "finish_reason": "stop"}

        client = ModelFallbackClient()
        messages = [
            {"role": "system", "content": "tool instructions"},
            {"role": "user", "content": "old history " * 20},
            {"role": "assistant", "content": "old answer " * 20},
            {"role": "user", "content": "Use Bash to run pwd."},
        ]

        with (
            patch.object(server, "FLOWITH_FABLE_CONTEXT_COMPACT_CHARS", 120),
            patch.object(
                server,
                "FLOWITH_FABLE_FALLBACK_MODEL",
                "claude-5-sonnet",
                create=True,
            ),
        ):
            events = b"".join(
                server._stream_claude_events(
                    client,
                    messages=messages,
                    requested_model="claude-fable-5",
                    upstream_model="claude-fable-5",
                    has_tools=True,
                )
            ).decode("utf-8")

        self.assertEqual(
            [call["model"] for call in client.calls],
            ["claude-fable-5", "claude-5-sonnet"],
        )
        self.assertEqual(client.calls[0]["messages"], client.calls[1]["messages"])
        self.assertIn('"type": "tool_use"', events)
        self.assertIn('"stop_reason": "tool_use"', events)
        self.assertNotIn("[proxy] upstream returned no content", events)

    def test_streaming_empty_long_context_retries_with_recent_messages(self) -> None:
        class ContextFallbackClient:
            def __init__(self) -> None:
                self.calls = []

            def call_api(self, messages, **kwargs):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    return {
                        "success": False,
                        "empty_response": True,
                        "error": "Upstream returned no content after the bounded retry budget.",
                    }
                kwargs["on_chunk"](
                    '<tool_call>\n<name>get_weather</name>\n'
                    '<parameters>{"city":"Shanghai"}</parameters>\n</tool_call>'
                )
                return {"success": True, "content": "", "usage": {}, "finish_reason": "stop"}

        client = ContextFallbackClient()
        messages = [
            {"role": "system", "content": "tool instructions"},
            {"role": "user", "content": "old history " * 20},
            {"role": "assistant", "content": "old answer " * 20},
            {"role": "user", "content": "Use get_weather for Shanghai."},
        ]

        with patch.object(server, "FLOWITH_EMPTY_CONTEXT_FALLBACK_CHARS", 120, create=True):
            events = b"".join(
                server._stream_claude_events(
                    client,
                    messages=messages,
                    requested_model="claude-fable-5",
                    has_tools=True,
                )
            ).decode("utf-8")

        self.assertEqual(len(client.calls), 2)
        retried_messages = client.calls[1]
        self.assertEqual(retried_messages[0]["role"], "system")
        self.assertIn("Shanghai", retried_messages[-1]["content"])
        self.assertNotIn("old history", "\n".join(m["content"] for m in retried_messages))
        self.assertIn('"type": "tool_use"', events)
        self.assertIn('"stop_reason": "tool_use"', events)

    def test_fable_long_context_is_compacted_before_the_first_upstream_call(self) -> None:
        class CapturingClient:
            def __init__(self) -> None:
                self.calls = []

            def call_api(self, messages, **kwargs):
                self.calls.append(messages)
                kwargs["on_chunk"](
                    '<tool_call>\n<name>get_weather</name>\n'
                    '<parameters>{"city":"Shanghai"}</parameters>\n</tool_call>'
                )
                return {"success": True, "content": "", "usage": {}, "finish_reason": "stop"}

        client = CapturingClient()
        messages = [
            {"role": "system", "content": "tool instructions"},
            {"role": "user", "content": "old history " * 20},
            {"role": "assistant", "content": "old answer " * 20},
            {"role": "user", "content": "Use get_weather for Shanghai."},
        ]

        with patch.object(server, "FLOWITH_FABLE_CONTEXT_COMPACT_CHARS", 120, create=True):
            events = b"".join(
                server._stream_claude_events(
                    client,
                    messages=messages,
                    requested_model="claude-fable-5",
                    upstream_model="claude-fable-5",
                    has_tools=True,
                )
            ).decode("utf-8")

        self.assertEqual(len(client.calls), 1)
        sent_messages = client.calls[0]
        self.assertIn("Shanghai", sent_messages[-1]["content"])
        self.assertNotIn("old history", "\n".join(m["content"] for m in sent_messages))
        self.assertIn('"type": "tool_use"', events)

    def test_context_compaction_keeps_a_contiguous_recent_suffix(self) -> None:
        messages = [
            {"role": "system", "content": "tool instructions"},
            {"role": "user", "content": "stale request"},
            {"role": "assistant", "content": "large intermediate output " * 20},
            {"role": "user", "content": "current request must use Bash"},
        ]

        compacted = server._recent_context_fallback_messages(messages, limit=100)

        self.assertEqual(
            compacted,
            [messages[0], messages[-1]],
        )

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
