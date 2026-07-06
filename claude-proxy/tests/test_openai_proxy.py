import io
import os
import unittest
from contextlib import redirect_stdout

from fastapi.testclient import TestClient

from proxy import server


class FakeFlowithClient:
    def __init__(self) -> None:
        self.calls = []
        self.next_result = None
        self.next_stream_chunks = None

    def call_api(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if kwargs.get("stream"):
            chunks = self.next_stream_chunks or ["hel", "lo"]
            for chunk in chunks:
                kwargs["on_chunk"](chunk)
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
                        "model": "claude-4.6-sonnet",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                ),
                (
                    "/v1/responses",
                    {
                        "model": "claude-4.6-sonnet",
                        "input": "hi",
                    },
                ),
                (
                    "/v1/messages",
                    {
                        "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
            },
        )

        self.assertEqual(response.status_code, 404)

    def test_chat_completions_non_streaming(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-4.6-sonnet",
                "messages": [{"role": "user", "content": "say hello"}],
                "max_tokens": 16,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["model"], "claude-4.6-sonnet")
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
                    "model": "claude-4.6-sonnet",
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
        self.assertIn("model=claude-4.6-sonnet", log)
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
                    "model": "claude-4.6-sonnet",
                    "input": "private response input",
                    "stream": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        log = stdout.getvalue()
        self.assertIn("[REQ] route=responses", log)
        self.assertIn("path=/v1/responses", log)
        self.assertIn("model=claude-4.6-sonnet", log)
        self.assertIn("tools=0", log)
        self.assertIn("msgs=1", log)
        self.assertIn("stream=True", log)
        self.assertNotIn("private response input", log)

    def test_chat_completions_streaming(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
                "input": "say hello",
                "max_output_tokens": 16,
            },
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["model"], "claude-4.6-sonnet")
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
                "model": "claude-4.6-sonnet",
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

    def test_responses_streaming_preserves_final_partial_think_prefix(self) -> None:
        self.fake_client.next_stream_chunks = ["answer <th"]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-4.6-sonnet",
                "input": "echo final tail",
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('"delta": "answer "', response.text)
        self.assertIn('"delta": "<th"', response.text)
        self.assertIn('"output_text": "answer <th"', response.text)

    def test_responses_streaming_with_tools_preserves_final_partial_tool_prefix(self) -> None:
        self.fake_client.next_stream_chunks = ["answer <tool_call"]

        response = self.client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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
                "model": "claude-4.6-sonnet",
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

    def test_hermes_root_chat_completions_alias(self) -> None:
        response = self.client.post(
            "/chat/completions",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-4.6-sonnet",
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
                    "model": "claude-4.6-sonnet",
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
                b'{"model":"claude-4.6-sonnet","messages":'
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
