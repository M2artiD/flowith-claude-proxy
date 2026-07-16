import io
import json
import os
import queue
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from proxy import server


class FakeFlowithClient:
    def __init__(self) -> None:
        self.calls = []
        self.next_result = None
        self.next_stream_chunks = None
        self.next_stream_result = None

    def call_api(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if kwargs.get("stream"):
            chunks = self.next_stream_chunks or ["hel", "lo"]
            for chunk in chunks:
                kwargs["on_chunk"](chunk)
            if self.next_stream_result is not None:
                return self.next_stream_result
            return {
                "success": True,
                "content": "".join(chunks),
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
                "finish_reason": "stop",
            }
        if self.next_result is not None:
            return self.next_result
        return {
            "success": True,
            "content": "hello",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            "finish_reason": "stop",
        }


class OpenAIProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_server_api_key = server._SERVER_API_KEY
        self.original_default_client = server._default_client
        self.original_profile = os.environ.get("FLOWITH_API_PROFILE")
        self.original_request_log = server.FLOWITH_REQUEST_LOG

        self.fake_client = FakeFlowithClient()
        server._SERVER_API_KEY = "test-key"
        server._default_client = self.fake_client
        server.FLOWITH_REQUEST_LOG = False
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server._SERVER_API_KEY = self.original_server_api_key
        server._default_client = self.original_default_client
        server.FLOWITH_REQUEST_LOG = self.original_request_log
        if self.original_profile is None:
            os.environ.pop("FLOWITH_API_PROFILE", None)
        else:
            os.environ["FLOWITH_API_PROFILE"] = self.original_profile

    def test_require_server_key_rejects_anonymous_call(self) -> None:
        # With the gate on, an unauthenticated caller must not silently inherit
        # the server key via the header-less fallback. Regression for #issue-auth-fallback.
        original_flag = server.FLOWITH_REQUIRE_SERVER_KEY
        server.FLOWITH_REQUIRE_SERVER_KEY = True
        try:
            self.assertIsNone(server._resolve_api_key(None, None))
        finally:
            server.FLOWITH_REQUIRE_SERVER_KEY = original_flag

    def test_require_server_key_off_still_allows_fallback(self) -> None:
        original_flag = server.FLOWITH_REQUIRE_SERVER_KEY
        original_local_only = server.FLOWITH_LOCAL_ONLY
        server.FLOWITH_REQUIRE_SERVER_KEY = False
        server.FLOWITH_LOCAL_ONLY = True
        try:
            self.assertEqual(server._resolve_api_key(None, None), "test-key")
        finally:
            server.FLOWITH_REQUIRE_SERVER_KEY = original_flag
            server.FLOWITH_LOCAL_ONLY = original_local_only

    def test_server_key_fallback_is_disabled_when_not_local_only(self) -> None:
        original_flag = server.FLOWITH_REQUIRE_SERVER_KEY
        original_local_only = server.FLOWITH_LOCAL_ONLY
        server.FLOWITH_REQUIRE_SERVER_KEY = False
        server.FLOWITH_LOCAL_ONLY = False
        try:
            self.assertIsNone(server._resolve_api_key(None, None))
        finally:
            server.FLOWITH_REQUIRE_SERVER_KEY = original_flag
            server.FLOWITH_LOCAL_ONLY = original_local_only

    def test_require_server_key_rejects_wrong_key_on_all_inference_endpoints(self) -> None:
        # HTTP-level regression: with the gate on, a wrong key must get 401 on
        # every inference endpoint, and the correct key must still pass. Guards
        # against any single route dropping the injected require_api_key check.
        original_flag = server.FLOWITH_REQUIRE_SERVER_KEY
        server.FLOWITH_REQUIRE_SERVER_KEY = True
        try:
            inference_requests = [
                (
                    "/v1/chat/completions",
                    {
                        "model": "claude-5-sonnet",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ),
                (
                    "/v1/responses",
                    {
                        "model": "claude-5-sonnet",
                        "input": "hi",
                    },
                ),
                (
                    "/v1/messages",
                    {
                        "model": "claude-5-sonnet",
                        "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ),
            ]
            for path, payload in inference_requests:
                with self.subTest(path=path, key="wrong"):
                    response = self.client.post(
                        path,
                        headers={"Authorization": "Bearer wrong-key"},
                        json=payload,
                    )
                    self.assertEqual(response.status_code, 401)
                with self.subTest(path=path, key="correct"):
                    response = self.client.post(
                        path,
                        headers={"Authorization": "Bearer test-key"},
                        json=payload,
                    )
                    self.assertEqual(response.status_code, 200)
        finally:
            server.FLOWITH_REQUIRE_SERVER_KEY = original_flag

    def test_claude_profile_disables_openai_endpoints(self) -> None:
        os.environ["FLOWITH_API_PROFILE"] = "claude"

        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_chat_completions_non_streaming(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
                "max_tokens": 16,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "claude-5-sonnet")
        self.assertEqual(body["choices"][0]["message"]["content"], "hello")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(self.fake_client.calls[0]["messages"], [{"role": "user", "content": "say hello"}])

    def test_chat_completions_request_log_summarizes_without_prompt_content(self) -> None:
        server.FLOWITH_REQUEST_LOG = True
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "claude-5-sonnet",
                    "messages": [{"role": "user", "content": "secret prompt text"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "shell",
                                "description": "Run a shell command",
                                "parameters": {"type": "object"},
                            },
                        }
                    ],
                    "stream": False,
                },
            )

        self.assertEqual(response.status_code, 200)
        log = stdout.getvalue()
        self.assertIn("[REQ] route=chat_completions", log)
        self.assertIn("path=/v1/chat/completions", log)
        self.assertIn("model=claude-5-sonnet", log)
        self.assertIn("tools=1", log)
        self.assertIn("msgs=1", log)
        self.assertIn("stream=False", log)
        self.assertNotIn("secret prompt text", log)

    def test_responses_request_log_summarizes_without_input_content(self) -> None:
        server.FLOWITH_REQUEST_LOG = True
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            response = self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "claude-5-sonnet",
                    "input": "private response input",
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        log = stdout.getvalue()
        self.assertIn("[REQ] route=responses", log)
        self.assertIn("path=/v1/responses", log)
        self.assertIn("model=claude-5-sonnet", log)
        self.assertIn("tools=0", log)
        self.assertIn("msgs=1", log)
        self.assertIn("stream=True", log)
        self.assertNotIn("private response input", log)

    def test_chat_completions_streaming(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('"object": "chat.completion.chunk"', events)
        self.assertIn('"content": "hel"', events)
        self.assertIn('"content": "lo"', events)
        self.assertIn('"finish_reason": "stop"', events)
        self.assertIn("data: [DONE]", events)

    def test_chat_completions_streaming_preserves_final_partial_think_prefix(self) -> None:
        self.fake_client.next_stream_chunks = ["answer <th"]

        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "echo final tail"}],
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"content": "answer "', response.text)
        self.assertIn('"content": "<th"', response.text)

    def test_chat_completions_streaming_with_tools_does_not_duplicate_streamed_plain_text(self) -> None:
        self.fake_client.next_stream_chunks = ["hello"]

        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertEqual(events.count('\"content\": \"hello\"'), 1)
        self.assertIn('\"finish_reason\": \"stop\"', events)
        self.assertIn('data: [DONE]', events)

    def test_chat_completions_streaming_with_tools_preserves_plain_text(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('"content": "hel"', events)
        self.assertIn('"content": "lo"', events)
        self.assertIn('"finish_reason": "stop"', events)
        self.assertIn("data: [DONE]", events)
        self.assertNotIn('"finish_reason": "tool_calls"', events)

    def test_chat_completions_streaming_with_tools_preserves_final_partial_tool_prefix(self) -> None:
        self.fake_client.next_stream_chunks = ["answer <tool_call"]

        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "echo final tail"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"content": "answer "', response.text)
        self.assertIn('"content": "<tool_call"', response.text)
        self.assertIn('"finish_reason": "stop"', response.text)
        self.assertNotIn('"finish_reason": "tool_calls"', response.text)

    def test_chat_completions_streaming_with_tools_xml_tool_call_does_not_leak_raw_xml(self) -> None:
        self.fake_client.next_stream_chunks = [
            "<tool_call>\n<name>shell</name>\n",
            "<parameters>\n",
            '{"command":"pwd"}',
            "\n</parameters>\n</tool_call>",
        ]

        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "check cwd"}],
                "stream": True,
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "shell",
                            "description": "Run a shell command",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('"tool_calls"', events)
        self.assertIn('"name": "shell"', events)
        self.assertIn('"{\\"command\\":\\"pwd\\"}"', events)
        self.assertIn('"finish_reason": "tool_calls"', events)
        self.assertNotIn("<tool_call>", events)
        self.assertNotIn('"content": "<tool_call>', events)

    def test_openai_streaming_preserves_unicode_text(self) -> None:
        self.fake_client.next_stream_chunks = ["??", "???"]

        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('??', events)
        self.assertIn('???', events)

    def test_responses_streaming_preserves_unicode_text(self) -> None:
        self.fake_client.next_stream_chunks = ["??", "???"]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "say hello",
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('??', events)
        self.assertIn('???', events)

    def test_chat_completions_tool_history_becomes_xml(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.5",
                "messages": [
                    {"role": "user", "content": "check cwd"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "shell",
                                    "arguments": '{"command":"pwd"}',
                                },
                            }
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_abc",
                        "content": "C:\\Users\\qiyan\\Desktop\\flowith-claude-proxy",
                    },
                ],
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        messages = self.fake_client.calls[0]["messages"]
        self.assertEqual(messages[1]["role"], "user")
        self.assertEqual(messages[1]["content"], "check cwd")
        self.assertEqual(messages[2]["role"], "assistant")
        self.assertIn("<tool_call>", messages[2]["content"])
        self.assertIn("<name>shell</name>", messages[2]["content"])
        self.assertIn('"command":"pwd"', messages[2]["content"])
        self.assertEqual(messages[3]["role"], "user")
        self.assertIn("<observation>", messages[3]["content"])
        self.assertIn("flowith-claude-proxy", messages[3]["content"])

    def test_responses_non_streaming(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "say hello",
                "max_output_tokens": 16,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["model"], "claude-5-sonnet")
        self.assertEqual(body["output_text"], "hello")
        self.assertEqual(body["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(self.fake_client.calls[0]["messages"], [{"role": "user", "content": "say hello"}])

    def test_responses_non_streaming_xml_tool_call(self) -> None:
        self.fake_client.next_result = {
            "success": True,
            "content": (
                "<tool_call>\n"
                "<name>shell</name>\n"
                "<parameters>\n"
                '{"command":"pwd"}\n'
                "</parameters>\n"
                "</tool_call>"
            ),
            "usage": {"prompt_tokens": 9, "completion_tokens": 4},
            "finish_reason": "stop",
        }

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.5",
                "input": "check cwd",
                "tools": [
                    {
                        "type": "function",
                        "name": "shell",
                        "description": "Run a shell command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["output_text"], "")
        tool_item = body["output"][0]
        self.assertEqual(tool_item["type"], "function_call")
        self.assertEqual(tool_item["name"], "shell")
        self.assertEqual(tool_item["arguments"], '{"command":"pwd"}')
        self.assertTrue(tool_item["call_id"].startswith("call_"))

        upstream_call = self.fake_client.calls[0]
        self.assertIsNone(upstream_call.get("tools"))
        self.assertNotIn("</tool_call>", upstream_call.get("stop_sequences") or [])
        self.assertEqual(upstream_call["messages"][0]["role"], "system")
        self.assertIn("# Tool Use", upstream_call["messages"][0]["content"])
        self.assertIn("Use a tool only when it is needed", upstream_call["messages"][0]["content"])
        self.assertIn("### shell", upstream_call["messages"][0]["content"])

    def test_responses_gpt_5_6_first_turn_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "run a command",
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        prompt = self.fake_client.calls[0]["messages"][0]["content"]
        self.assertIn("You must call one available tool.", prompt)
        self.assertEqual(self.fake_client.calls[0]["model"], "gpt-5.6-sol")

    def test_responses_gpt_5_6_code_request_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "code a 3d pokeball in one html file and save it to the desktop",
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertEqual(last_message["role"], "system")
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_mandatory_tool_phrase_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": (
                    "这是端到端验收。必须实际调用 shell_command 执行 PowerShell "
                    "命令 Get-Location；不要仅描述命令。"
                ),
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_use_tool_to_run_phrase_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "Use shell_command to run Get-Location.",
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_object_fronted_workspace_write_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "这些话为我提炼成skills写在当前工作区",
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_greeting_does_not_require_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "你好",
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        messages = self.fake_client.calls[0]["messages"]
        self.assertFalse(any("TOOL CALL REQUIRED FOR THIS TURN" in message["content"] for message in messages))

    def test_responses_gpt_5_6_explanation_question_does_not_require_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "这是什么意思？请直接解释。",
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        messages = self.fake_client.calls[0]["messages"]
        self.assertFalse(any("TOOL CALL REQUIRED FOR THIS TURN" in message["content"] for message in messages))

    def test_responses_gpt_5_6_quoted_action_explanation_does_not_require_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "‘为我创建一个技能’是什么意思？只解释这句话。",
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        messages = self.fake_client.calls[0]["messages"]
        self.assertFalse(any("TOOL CALL REQUIRED FOR THIS TURN" in message["content"] for message in messages))

    def test_responses_gpt_5_6_explanation_plus_explicit_repair_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "为什么会失败？请帮我修复它。",
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_negated_action_does_not_require_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "不要运行命令，只解释 Get-Location 是什么意思。",
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        messages = self.fake_client.calls[0]["messages"]
        self.assertFalse(any("TOOL CALL REQUIRED FOR THIS TURN" in message["content"] for message in messages))

    def test_responses_gpt_5_6_required_tool_rule_is_last_message(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "open cmd",
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertEqual(last_message["role"], "system")
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])
        self.assertIn("concise user-visible action note", last_message["content"])
        self.assertIn("exact command", last_message["content"])
        self.assertIn("then output the XML tool call", last_message["content"])
        self.assertIn("must not include a result, success/failure claim, or final answer", last_message["content"])

    def test_responses_gpt_5_6_terse_continuation_inherits_prior_action(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "build an offline interactive HTML file"},
                    {"type": "message", "role": "assistant", "content": "I can do that."},
                    {"type": "message", "role": "user", "content": "去吧"},
                ],
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_chinese_write_then_terse_continuation_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "写一个互动 HTML 到桌面"},
                    {"type": "message", "role": "assistant", "content": "我会处理。"},
                    {"type": "message", "role": "user", "content": "去吧"},
                ],
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_colloquial_continue_inherits_object_fronted_action(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "这些话为我提炼成skills写在当前工作区",
                    },
                    {"type": "message", "role": "assistant", "content": "我会先检查技能规范。"},
                    {"type": "message", "role": "user", "content": "继续啊"},
                ],
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_failure_report_inherits_prior_action(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "create and verify an interactive HTML file"},
                    {"type": "message", "role": "assistant", "content": "The file is ready."},
                    {"type": "message", "role": "user", "content": "3D 引擎未能载入"},
                ],
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_failure_explanation_question_does_not_force_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "create an interactive HTML file"},
                    {"type": "message", "role": "assistant", "content": "The file is ready."},
                    {"type": "message", "role": "user", "content": "为什么 3D 引擎未能载入？只解释原因。"},
                ],
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        messages = self.fake_client.calls[0]["messages"]
        self.assertFalse(any("TOOL CALL REQUIRED FOR THIS TURN" in message["content"] for message in messages))

    def test_responses_gpt_5_6_new_user_turn_after_tool_history_requires_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "first action"},
                    {"type": "function_call", "call_id": "call_old", "name": "shell_command", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_old", "output": "done"},
                    {"type": "message", "role": "user", "content": "open cmd"},
                ],
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertEqual(last_message["role"], "system")
        self.assertIn("TOOL CALL REQUIRED FOR THIS TURN", last_message["content"])

    def test_responses_gpt_5_6_immediate_tool_result_does_not_force_another_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "function_call", "call_id": "call_now", "name": "shell_command", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_now", "output": "done"},
                ],
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        messages = self.fake_client.calls[0]["messages"]
        self.assertFalse(any("TOOL CALL REQUIRED FOR THIS TURN" in message["content"] for message in messages))

    def test_responses_gpt_5_6_tool_result_followup_forbids_progress_only_completion(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "build the requested file and open it"},
                    {"type": "function_call", "call_id": "call_now", "name": "shell_command", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_now", "output": "project rules read"},
                ],
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertEqual(last_message["role"], "system")
        self.assertIn("TOOL RESULT FOLLOW-UP", last_message["content"])
        self.assertIn("progress update or future-tense promise", last_message["content"])
        self.assertIn("call the next available tool now", last_message["content"])
        self.assertIn("user-visible action note", last_message["content"])
        self.assertIn("exact command", last_message["content"])
        self.assertIn("must not include a result, success/failure claim, or final answer", last_message["content"])

    def test_responses_tool_result_followup_ignores_trailing_reasoning_item(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "finish the file"},
                    {"type": "function_call", "call_id": "call_now", "name": "shell_command", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_now", "output": "download failed"},
                    {"type": "reasoning", "summary": []},
                ],
                "tools": [{"type": "function", "name": "shell_command", "parameters": {"type": "object"}}],
            },
        )

        last_message = self.fake_client.calls[0]["messages"][-1]
        self.assertIn("TOOL RESULT FOLLOW-UP", last_message["content"])

    def test_responses_custom_tool_output_becomes_followup_observation(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": [
                    {"type": "message", "role": "user", "content": "finish the file"},
                    {
                        "type": "custom_tool_call",
                        "call_id": "call_custom",
                        "name": "exec_command",
                        "input": '{"cmd":"Get-Location"}',
                    },
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_custom",
                        "output": "Process exited with code 1",
                    },
                    {"type": "reasoning", "summary": []},
                ],
                "tools": [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}],
            },
        )

        messages = self.fake_client.calls[0]["messages"]
        self.assertTrue(any("<observation>" in message["content"] for message in messages))
        self.assertIn("TOOL RESULT FOLLOW-UP", messages[-1]["content"])

    def test_responses_codex_5_6_compat_alias_requires_a_tool(self) -> None:
        self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.4-flowith-5.6",
                "input": "run a command",
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        prompt = self.fake_client.calls[0]["messages"][0]["content"]
        self.assertIn("You must call one available tool.", prompt)
        self.assertEqual(self.fake_client.calls[0]["model"], "gpt-5.6-sol")

    def test_responses_input_function_call_output_becomes_observation(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.5",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_abc",
                        "name": "shell",
                        "arguments": '{"command":"pwd"}',
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_abc",
                        "output": "C:\\Users\\qiyan\\Desktop\\flowith-claude-proxy",
                    },
                ],
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        messages = self.fake_client.calls[0]["messages"]
        self.assertIn("<tool_call>", messages[1]["content"])
        self.assertIn("<observation>", messages[2]["content"])
        self.assertIn("flowith-claude-proxy", messages[2]["content"])

    def test_responses_streaming(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "say hello"}]}],
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn("event: response.created", events)
        self.assertIn("event: response.output_text.delta", events)
        self.assertIn('"delta": "hel"', events)
        self.assertIn('"delta": "lo"', events)
        self.assertIn("event: response.completed", events)

    def test_responses_streaming_emits_protocol_heartbeat_while_upstream_is_idle(self) -> None:
        class OneIdleQueue(queue.Queue):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.forced_idle = False

            def get(self, block=True, timeout=None):
                if not self.forced_idle:
                    self.forced_idle = True
                    raise queue.Empty
                return super().get(block=block, timeout=timeout)

        isolated_queue_module = SimpleNamespace(
            Queue=OneIdleQueue,
            Empty=queue.Empty,
            Full=queue.Full,
        )
        with patch("proxy.codex.router.queue", isolated_queue_module):
            response = self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "gpt-5.6-sol",
                    "input": "say hello",
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: response.in_progress", response.text)
        self.assertIn('"status": "in_progress"', response.text)
        self.assertIn("event: response.completed", response.text)

    def test_hermes_compact_final_events_do_not_repeat_streamed_text(self) -> None:
        with patch("proxy.codex.router.FLOWITH_RESPONSES_COMPACT_FINAL_TEXT", True, create=True):
            response = self.client.post(
                "/v1/responses",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "claude-5-sonnet",
                    "input": "say hello",
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('"delta": "hel"', events)
        self.assertIn('"delta": "lo"', events)
        self.assertNotIn('"text": "hello"', events)
        self.assertNotIn('"output_text": "hello"', events)

    def test_responses_streaming_preserves_final_partial_think_prefix(self) -> None:
        self.fake_client.next_stream_chunks = ["answer <th"]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "echo final tail",
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"delta": "answer "', response.text)
        self.assertIn('"delta": "<th"', response.text)
        self.assertIn('"output_text": "answer <th"', response.text)

    def test_responses_streaming_partial_upstream_failure_has_terminal_events(self) -> None:
        self.fake_client.next_stream_chunks = ["partial"]
        self.fake_client.next_stream_result = {
            "success": False,
            "content": "partial",
            "error": "Upstream stream ended before completion",
        }

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "echo partial failure",
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn("event: response.output_text.delta", events)
        self.assertIn('"delta": "partial"', events)
        self.assertIn("event: response.failed", events)
        self.assertIn('"status": "failed"', events)
        self.assertIn("Upstream stream ended before completion", events)
        self.assertTrue(events.rstrip().endswith("data: [DONE]"))

    def test_responses_streaming_with_tools_preserves_final_partial_tool_prefix(self) -> None:
        self.fake_client.next_stream_chunks = ["answer <tool_call"]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "echo final tail",
                "stream": True,
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"delta": "answer "', response.text)
        self.assertIn('"delta": "<tool_call"', response.text)
        self.assertIn('"output_text": "answer <tool_call"', response.text)
        self.assertNotIn('"type": "function_call"', response.text)

    def test_responses_streaming_xml_tool_call(self) -> None:
        self.fake_client.next_stream_chunks = [
            "<tool_call>\n<name>shell</name>\n",
            "<parameters>\n",
            '{"command":"dir"}',
            "\n</parameters>\n</tool_call>",
        ]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.5",
                "input": "list files",
                "stream": True,
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn("event: response.output_item.added", events)
        self.assertIn('"type": "function_call"', events)
        self.assertIn('"name": "shell"', events)
        self.assertIn("event: response.function_call_arguments.delta", events)
        self.assertIn('"delta": "{\\"command\\":\\"dir\\"}"', events)
        self.assertIn("event: response.function_call_arguments.done", events)
        self.assertIn("event: response.completed", events)
        self.assertNotIn("<tool_call>", events)

    def test_responses_streaming_with_tools_plain_text_keeps_final_text(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "say hello",
                "stream": True,
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn("event: response.created", events)
        self.assertIn("event: response.output_text.delta", events)
        self.assertIn('"delta": "hel"', events)
        self.assertIn('"delta": "lo"', events)
        self.assertIn("event: response.output_text.done", events)
        self.assertIn("event: response.completed", events)
        self.assertNotIn('"type": "function_call"', events)
        self.assertIn('"text": "hello"', events)
        self.assertIn('"output_text": "hello"', events)

    def test_responses_streaming_with_tools_xml_tool_call_does_not_leak_raw_xml(self) -> None:
        self.fake_client.next_stream_chunks = [
            "<tool_call>\n<name>shell</name>\n",
            "<parameters>\n",
            '{"command":"pwd"}',
            "\n</parameters>\n</tool_call>",
        ]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "check cwd",
                "stream": True,
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('"type": "function_call"', events)
        self.assertIn('"name": "shell"', events)
        self.assertIn('"delta": "{\\"command\\":\\"pwd\\"}"', events)
        self.assertIn("event: response.completed", events)
        self.assertNotIn("<tool_call>", events)

    def test_responses_streaming_with_tools_preserves_text_around_xml_tool_call(self) -> None:
        self.fake_client.next_stream_chunks = [
            "Need cwd. ",
            "<tool_call>\n<name>shell</name>\n",
            "<parameters>\n",
            '{"command":"pwd"}',
            "\n</parameters>\n</tool_call>",
            " Done.",
        ]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "check cwd and explain",
                "stream": True,
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn('"delta": "Need cwd. "', events)
        self.assertIn('"delta": " Done."', events)
        self.assertIn('"type": "function_call"', events)
        self.assertIn('"name": "shell"', events)
        self.assertIn('"text": "Need cwd.  Done."', events)
        self.assertIn('"output_text": "Need cwd.  Done."', events)
        self.assertNotIn("<tool_call>", events)

    def test_responses_gpt_5_6_stream_tool_feedback_discards_premature_final_text(self) -> None:
        self.fake_client.next_stream_chunks = [
            "To inspect the directory, use shell with exact command `pwd`.\n\nPREMATURE_OK",
            "<tool_call>\n<name>shell</name>\n",
            "<parameters>\n",
            '{"command":"pwd"}',
            "\n</parameters>\n</tool_call>",
        ]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "check cwd",
                "stream": True,
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn("To inspect the directory, use shell with exact command `pwd`.", events)
        self.assertNotIn("PREMATURE_OK", events)
        self.assertIn('"type": "function_call"', events)
        self.assertLess(
            events.index("event: response.output_text.done"),
            events.index('"type": "function_call"'),
        )

    def test_responses_forwards_high_reasoning_to_flowith_thinking(self) -> None:
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "explain the approach",
                "stream": True,
                "reasoning": {"effort": "high", "summary": "auto"},
            },
        )

        self.assertEqual(response.status_code, 200)
        call = self.fake_client.calls[0]
        self.assertIs(call["thinking"], True)
        self.assertGreater(call["thinking_budget_tokens"], 0)

    def test_responses_gpt_5_6_tool_feedback_preserves_bounded_deduplicated_plan(self) -> None:
        self.fake_client.next_stream_chunks = [
            (
                "执行计划：\n"
                "1. 使用 `shell` 执行精确命令 `pwd`。\n"
                "2. 根据真实输出继续。\n"
                "2. 根据真实输出继续。\n"
                "PREMATURE_OK"
            ),
            "<tool_call>\n<name>shell</name>\n",
            "<parameters>\n",
            '{"command":"pwd"}',
            "\n</parameters>\n</tool_call>",
        ]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "plan carefully, then check cwd",
                "stream": True,
                "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        self.assertIn("执行计划：", events)
        self.assertIn("1. 使用 `shell` 执行精确命令 `pwd`。", events)
        delta_text = "".join(
            json.loads(line[6:]).get("delta", "")
            for line in events.splitlines()
            if line.startswith("data: {")
            and json.loads(line[6:]).get("type") == "response.output_text.delta"
        )
        self.assertEqual(delta_text.count("2. 根据真实输出继续。"), 1)
        self.assertNotIn("PREMATURE_OK", events)
        self.assertIn('"type": "function_call"', events)

    def test_responses_gpt_5_6_tool_feedback_preserves_public_decision_brief(self) -> None:
        self.fake_client.next_stream_chunks = [
            (
                "决策摘要：\n"
                "- 判断：CDN 加载失败，必须消除网络依赖。\n"
                "- 取舍：内嵌引擎会增大文件，但换来真正离线运行。\n"
                "- 动作：使用 `shell_command` 下载并内嵌可用资源。\n"
                "- 验证：无头浏览器渲染并检查截图像素。\n"
                "PREMATURE_OK"
            ),
            "<tool_call>\n<name>shell_command</name>\n",
            "<parameters>\n",
            '{"command":"Invoke-WebRequest https://example.test/three.js"}',
            "\n</parameters>\n</tool_call>",
        ]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "repair the offline 3D page and explain the key tradeoff",
                "stream": True,
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        events = response.text
        for expected in ["判断：", "取舍：", "动作：", "验证："]:
            self.assertIn(expected, events)
        self.assertNotIn("PREMATURE_OK", events)
        self.assertIn('"type": "function_call"', events)

    def test_responses_required_action_corrects_one_no_tool_response_before_delivery(self) -> None:
        class CorrectingClient:
            def __init__(self) -> None:
                self.calls = []

            def call_api(self, messages, **kwargs):
                self.calls.append({"messages": [dict(message) for message in messages], **kwargs})
                if len(self.calls) == 1:
                    text = "I will open it later without using a tool."
                    kwargs["on_chunk"](text)
                    return {
                        "success": True,
                        "content": text,
                        "usage": {},
                        "finish_reason": "stop",
                    }

                chunks = [
                    "I will use `shell_command` with exact command `Start-Process cmd.exe`.",
                    "<tool_call>\n<name>shell_command</name>\n",
                    "<parameters>\n",
                    '{"command":"Start-Process cmd.exe"}',
                    "\n</parameters>\n</tool_call>",
                ]
                for chunk in chunks:
                    kwargs["on_chunk"](chunk)
                return {
                    "success": True,
                    "content": "".join(chunks),
                    "usage": {},
                    "finish_reason": "stop",
                }

        correcting_client = CorrectingClient()
        server._default_client = correcting_client
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "open cmd",
                "stream": True,
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(correcting_client.calls), 2)
        self.assertNotIn("without using a tool", response.text)
        self.assertIn('"type": "function_call"', response.text)
        self.assertTrue(
            any(
                "RETRY CORRECTION" in str(message.get("content", ""))
                for message in correcting_client.calls[1]["messages"]
            )
        )

    def test_responses_object_fronted_action_corrects_repeated_no_tool_promise(self) -> None:
        class CorrectingClient:
            def __init__(self) -> None:
                self.calls = []

            def call_api(self, messages, **kwargs):
                self.calls.append({"messages": [dict(message) for message in messages], **kwargs})
                if len(self.calls) == 1:
                    line = (
                        "我将用 `shell_command` 检查当前目录、技能脚手架与校验脚本，"
                        "具体执行 `Get-ChildItem`。"
                    )
                    text = f"{line}\n{line}"
                    kwargs["on_chunk"](text)
                    return {
                        "success": True,
                        "content": text,
                        "usage": {},
                        "finish_reason": "stop",
                    }

                chunks = [
                    "使用 `shell_command` 执行 `Get-ChildItem`，确认当前工作区结构。",
                    "<tool_call>\n<name>shell_command</name>\n",
                    "<parameters>\n",
                    '{"command":"Get-ChildItem"}',
                    "\n</parameters>\n</tool_call>",
                ]
                for chunk in chunks:
                    kwargs["on_chunk"](chunk)
                return {
                    "success": True,
                    "content": "".join(chunks),
                    "usage": {},
                    "finish_reason": "stop",
                }

        correcting_client = CorrectingClient()
        server._default_client = correcting_client
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "这些话为我提炼成skills写在当前工作区",
                "stream": True,
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(correcting_client.calls), 2)
        self.assertNotIn("技能脚手架与校验脚本", response.text)
        self.assertIn('"type": "function_call"', response.text)

    def test_responses_required_action_allows_two_corrections_before_failing(self) -> None:
        class TwiceAvoidingClient:
            def __init__(self) -> None:
                self.calls = []

            def call_api(self, messages, **kwargs):
                self.calls.append({"messages": [dict(message) for message in messages], **kwargs})
                if len(self.calls) < 3:
                    text = "I will perform the requested action later."
                    kwargs["on_chunk"](text)
                    return {"success": True, "content": text, "usage": {}, "finish_reason": "stop"}

                chunks = [
                    "决策摘要：\n- 动作：使用 `shell_command` 执行 `Get-Location`。",
                    "<tool_call>\n<name>shell_command</name>\n",
                    "<parameters>\n",
                    '{"command":"Get-Location"}',
                    "\n</parameters>\n</tool_call>",
                ]
                for chunk in chunks:
                    kwargs["on_chunk"](chunk)
                return {
                    "success": True,
                    "content": "".join(chunks),
                    "usage": {},
                    "finish_reason": "stop",
                }

        correcting_client = TwiceAvoidingClient()
        server._default_client = correcting_client
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "input": "run Get-Location",
                "stream": True,
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(correcting_client.calls), 3)
        self.assertNotIn("later", response.text)
        self.assertIn('"type": "function_call"', response.text)

    def test_responses_tool_result_followup_corrects_progress_without_next_tool(self) -> None:
        class FollowupCorrectingClient:
            def __init__(self) -> None:
                self.calls = []

            def call_api(self, messages, **kwargs):
                self.calls.append({"messages": [dict(message) for message in messages], **kwargs})
                if len(self.calls) == 1:
                    text = "我用 `exec_command` 读取前端验证规范，并检查桌面文件。"
                    kwargs["on_chunk"](text)
                    return {"success": True, "content": text, "usage": {}, "finish_reason": "stop"}

                chunks = [
                    "下一步使用 `shell_command` 执行精确命令 `Get-Item target.html`。",
                    "<tool_call>\n<name>shell_command</name>\n",
                    "<parameters>\n",
                    '{"command":"Get-Item target.html"}',
                    "\n</parameters>\n</tool_call>",
                ]
                for chunk in chunks:
                    kwargs["on_chunk"](chunk)
                return {
                    "success": True,
                    "content": "".join(chunks),
                    "usage": {},
                    "finish_reason": "stop",
                }

        correcting_client = FollowupCorrectingClient()
        server._default_client = correcting_client
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "stream": True,
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "创建文件并验证它"}],
                    },
                    {
                        "type": "function_call",
                        "name": "shell_command",
                        "arguments": '{"command":"Get-Location"}',
                        "call_id": "call_1",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "Process exited with code 0",
                    },
                ],
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(correcting_client.calls), 2)
        self.assertNotIn("读取前端验证规范", response.text)
        self.assertIn('"type": "function_call"', response.text)

    def test_responses_tool_result_followup_allows_observation_backed_final_answer(self) -> None:
        self.fake_client.next_stream_chunks = ["文件已写入并验证完成，退出码 0。"]
        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "gpt-5.6-sol",
                "stream": True,
                "input": [
                    {"type": "message", "role": "user", "content": "创建文件并验证它"},
                    {
                        "type": "function_call",
                        "name": "shell_command",
                        "arguments": '{"command":"Set-Content target.html ok"}',
                        "call_id": "call_1",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_1",
                        "output": "Process exited with code 0",
                    },
                ],
                "tools": [
                    {"type": "function", "name": "shell_command", "parameters": {"type": "object"}}
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.fake_client.calls), 1)
        self.assertIn("文件已写入并验证完成", response.text)
        self.assertNotIn('"type": "function_call"', response.text)

    def test_hermes_root_chat_completions_alias(self) -> None:
        response = self.client.post(
            "/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["choices"][0]["message"]["content"], "hello")
        self.assertEqual(self.fake_client.calls[0]["messages"], [{"role": "user", "content": "say hello"}])

    def test_hermes_probe_endpoints_do_not_404(self) -> None:
        for path in [
            "/models",
            "/v1/models",
            "/api/v1/models",
            "/api/tags",
            "/version",
            "/api/version",
            "/props",
            "/v1/props",
            "/api/props",
        ]:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)

    def test_require_server_key_rejects_wrong_key_on_discovery_endpoints(self) -> None:
        # Discovery endpoints can reveal configured model aliases/capabilities.
        # Keep them unauthenticated for local probes by default, but protect
        # them when the operator explicitly enables the server-key gate.
        original_flag = server.FLOWITH_REQUIRE_SERVER_KEY
        server.FLOWITH_REQUIRE_SERVER_KEY = True
        try:
            for path in [
                "/models",
                "/v1/models",
                "/api/v1/models",
                "/api/tags",
                "/version",
                "/api/version",
                "/props",
                "/v1/props",
                "/api/props",
            ]:
                with self.subTest(path=path, key="missing"):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 401)
                with self.subTest(path=path, key="wrong"):
                    response = self.client.get(
                        path,
                        headers={"Authorization": "Bearer wrong-key"},
                    )
                    self.assertEqual(response.status_code, 401)
                with self.subTest(path=path, key="correct"):
                    response = self.client.get(
                        path,
                        headers={"Authorization": "Bearer test-key"},
                    )
                    self.assertEqual(response.status_code, 200)
                with self.subTest(path=path, key="correct-x-api-key"):
                    response = self.client.get(
                        path,
                        headers={"x-api-key": "test-key"},
                    )
                    self.assertEqual(response.status_code, 200)
        finally:
            server.FLOWITH_REQUIRE_SERVER_KEY = original_flag

    def test_root_head_probe_succeeds(self) -> None:
        response = self.client.head("/")
        self.assertEqual(response.status_code, 200)

    def test_root_does_not_disclose_upstream_url(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("upstream", response.json())

    def test_request_body_over_configured_limit_returns_413(self) -> None:
        original_limit = server.FLOWITH_MAX_REQUEST_BYTES
        server.FLOWITH_MAX_REQUEST_BYTES = 32
        try:
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-key"},
                json={
                    "model": "claude-5-sonnet",
                    "messages": [{"role": "user", "content": "x" * 64}],
                },
            )
            self.assertEqual(response.status_code, 413)
        finally:
            server.FLOWITH_MAX_REQUEST_BYTES = original_limit

    def test_request_body_without_content_length_over_limit_returns_413(self) -> None:
        # Regression: chunked or otherwise length-less requests must not bypass
        # the request-size guard and then be read fully by request.json().
        original_limit = server.FLOWITH_MAX_REQUEST_BYTES
        server.FLOWITH_MAX_REQUEST_BYTES = 32
        try:
            body = (
                b'{"model":"claude-5-sonnet","messages":'
                b'[{"role":"user","content":"'
                + (b"x" * 64)
                + b'"}]}'
            )
            response = self.client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-key",
                    "transfer-encoding": "chunked",
                },
                content=iter([body]),
            )
            self.assertEqual(response.status_code, 413)
        finally:
            server.FLOWITH_MAX_REQUEST_BYTES = original_limit


if __name__ == "__main__":
    unittest.main()
