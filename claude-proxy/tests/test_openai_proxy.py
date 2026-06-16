import os
import unittest

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

        self.fake_client = FakeFlowithClient()
        server._SERVER_API_KEY = "test-key"
        server._default_client = self.fake_client
        self.client = TestClient(server.app)

    def tearDown(self) -> None:
        server._SERVER_API_KEY = self.original_server_api_key
        server._default_client = self.original_default_client
        if self.original_profile is None:
            os.environ.pop("FLOWITH_API_PROFILE", None)
        else:
            os.environ["FLOWITH_API_PROFILE"] = self.original_profile

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
        self.assertIn("</tool_call>", upstream_call.get("stop_sequences"))
        self.assertEqual(upstream_call["messages"][0]["role"], "system")
        self.assertIn("# TOOL CALLING", upstream_call["messages"][0]["content"])
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


if __name__ == "__main__":
    unittest.main()
