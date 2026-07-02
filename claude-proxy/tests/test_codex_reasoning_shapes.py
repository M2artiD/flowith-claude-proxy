"""Regression: codex path must not swallow reasoning when the upstream uses
Anthropic-style content blocks or non-standard reasoning field names.
"""

import json
import unittest

from fastapi.testclient import TestClient

from proxy import server
from proxy import upstream as upstream_mod


class DirectFlowithClient:
    """Non-stream fake that returns raw upstream JSON, exercising _extract helpers."""

    def __init__(self, message_payload):
        self.message_payload = message_payload

    def call_api(self, messages, **kwargs):
        msg = dict(self.message_payload)
        content_val, thinking_from_blocks = upstream_mod._extract_content_and_thinking(
            msg.get("content")
        )
        reasoning_text = upstream_mod._pick_reasoning_field(msg) or thinking_from_blocks
        return {
            "success": True,
            "content": content_val,
            "reasoning_content": reasoning_text,
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            "finish_reason": "stop",
        }


def parse_sse(raw_body: str):
    events = []
    for block in raw_body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :].strip()
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
        data_str = "\n".join(data_lines)
        try:
            data = json.loads(data_str)
        except Exception:
            data = data_str
        events.append((event, data))
    return events


class ExtractHelperTests(unittest.TestCase):
    def test_string_content_passthrough(self):
        self.assertEqual(
            upstream_mod._extract_content_and_thinking("hello"),
            ("hello", ""),
        )

    def test_anthropic_style_blocks(self):
        blocks = [
            {"type": "thinking", "thinking": "plan A then plan B"},
            {"type": "text", "text": "the final answer"},
        ]
        text, thinking = upstream_mod._extract_content_and_thinking(blocks)
        self.assertEqual(text, "the final answer")
        self.assertEqual(thinking, "plan A then plan B")

    def test_none_content(self):
        self.assertEqual(upstream_mod._extract_content_and_thinking(None), ("", ""))

    def test_pick_reasoning_prefers_reasoning_content(self):
        self.assertEqual(
            upstream_mod._pick_reasoning_field(
                {"reasoning_content": "a", "reasoning": "b", "thinking": "c"}
            ),
            "a",
        )

    def test_pick_reasoning_falls_back_to_alt_names(self):
        self.assertEqual(
            upstream_mod._pick_reasoning_field({"reasoning": "only reasoning"}),
            "only reasoning",
        )
        self.assertEqual(
            upstream_mod._pick_reasoning_field({"thinking": "only thinking"}),
            "only thinking",
        )

    def test_pick_reasoning_dict_shape(self):
        self.assertEqual(
            upstream_mod._pick_reasoning_field(
                {"reasoning": {"type": "thinking", "thinking": "deep thoughts"}}
            ),
            "deep thoughts",
        )


class CodexUpstreamShapeTests(unittest.TestCase):
    def setUp(self):
        self.original_key = server._SERVER_API_KEY
        self.original_client = server._default_client
        server._SERVER_API_KEY = "test-key"

    def tearDown(self):
        server._SERVER_API_KEY = self.original_key
        server._default_client = self.original_client

    def _run(self, fake):
        server._default_client = fake
        client = TestClient(server.app)
        resp = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-4.6-sonnet",
                "input": "say hello",
                "stream": True,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return parse_sse(resp.text)

    def test_anthropic_blocks_produce_reasoning_and_answer(self):
        fake = DirectFlowithClient(
            {
                "content": [
                    {"type": "thinking", "thinking": "internal plan text"},
                    {"type": "text", "text": "user visible answer"},
                ],
            }
        )
        events = self._run(fake)
        reasoning = "".join(
            d.get("delta", "")
            for e, d in events
            if e == "response.reasoning_summary_text.delta"
        )
        answer = "".join(
            d.get("delta", "")
            for e, d in events
            if e == "response.output_text.delta"
        )
        self.assertIn("internal plan text", reasoning)
        self.assertIn("user visible answer", answer)

    def test_alt_reasoning_field_is_recovered(self):
        fake = DirectFlowithClient(
            {
                "content": "final answer",
                "reasoning": "chain of thought via alt field",
            }
        )
        events = self._run(fake)
        reasoning = "".join(
            d.get("delta", "")
            for e, d in events
            if e == "response.reasoning_summary_text.delta"
        )
        answer = "".join(
            d.get("delta", "")
            for e, d in events
            if e == "response.output_text.delta"
        )
        self.assertIn("chain of thought via alt field", reasoning)
        self.assertIn("final answer", answer)


if __name__ == "__main__":
    unittest.main()
