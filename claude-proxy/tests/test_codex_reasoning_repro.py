"""Repro: /v1/responses (codex path) must not swallow <think> or answer.

Simulates upstream streaming a chunk that contains <think>...</think> and
final answer text, and asserts that:
  - reasoning_summary_text.delta events carry the thinking content
  - output_text.delta events carry the final answer
  - response.completed carries both in output[]
"""

import json
import unittest

from fastapi.testclient import TestClient

from proxy import server


class FakeStreamingFlowithClient:
    def __init__(self, chunks, reasoning_chunks=None, final_reasoning="", final_content=None) -> None:
        self.chunks = chunks
        self.reasoning_chunks = reasoning_chunks or []
        self.final_reasoning = final_reasoning
        self.final_content = final_content
        self.calls = []

    def call_api(self, messages, **kwargs):
        self.calls.append({"messages": messages, **{k: v for k, v in kwargs.items() if k not in ('on_chunk','on_reasoning')}})
        on_chunk = kwargs.get("on_chunk")
        on_reasoning = kwargs.get("on_reasoning")
        if on_reasoning:
            for rc in self.reasoning_chunks:
                on_reasoning(rc)
        if on_chunk:
            for c in self.chunks:
                on_chunk(c)
        content = self.final_content if self.final_content is not None else "".join(self.chunks)
        return {
            "success": True,
            "content": content,
            "reasoning_content": self.final_reasoning,
            "usage": {"prompt_tokens": 3, "completion_tokens": 5},
            "finish_reason": "stop",
        }


def parse_sse(raw_body: str):
    """Return list of (event, json_data_or_str)."""
    events = []
    for block in raw_body.split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        event = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        data_str = "\n".join(data_lines)
        try:
            data = json.loads(data_str)
        except Exception:
            data = data_str
        events.append((event, data))
    return events


class CodexReasoningReproTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_key = server._SERVER_API_KEY
        self.original_client = server._default_client
        server._SERVER_API_KEY = "test-key"

    def tearDown(self) -> None:
        server._SERVER_API_KEY = self.original_key
        server._default_client = self.original_client

    def _run_responses_stream(self, fake):
        server._default_client = fake
        client = TestClient(server.app)
        resp = client.post(
            "/v1/responses",
            headers={"Authorization": "Bearer test-key"},
            json={
                "model": "claude-5-sonnet",
                "input": "say hello",
                "stream": True,
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return parse_sse(resp.text)

    def test_think_block_from_content_stream_becomes_reasoning(self):
        # Upstream emits <think>...</think> inline in content stream.
        fake = FakeStreamingFlowithClient(
            chunks=["<think>plan step A</think>", "final answer text"],
        )
        events = self._run_responses_stream(fake)

        reasoning_deltas = [d for e, d in events if e == "response.reasoning_summary_text.delta"]
        text_deltas = [d for e, d in events if e == "response.output_text.delta"]

        reasoning_text = "".join(x.get("delta", "") for x in reasoning_deltas)
        answer_text = "".join(x.get("delta", "") for x in text_deltas)

        self.assertIn("plan step A", reasoning_text, f"reasoning lost: events={[e for e,_ in events]}")
        self.assertIn("final answer text", answer_text, f"answer lost: events={[e for e,_ in events]}")

    def test_reasoning_channel_from_upstream_is_preserved(self):
        # Upstream emits reasoning via on_reasoning callback (native reasoning channel).
        fake = FakeStreamingFlowithClient(
            chunks=["final answer text"],
            reasoning_chunks=["native reasoning body"],
        )
        events = self._run_responses_stream(fake)

        reasoning_deltas = [d for e, d in events if e == "response.reasoning_summary_text.delta"]
        text_deltas = [d for e, d in events if e == "response.output_text.delta"]

        reasoning_text = "".join(x.get("delta", "") for x in reasoning_deltas)
        answer_text = "".join(x.get("delta", "") for x in text_deltas)

        self.assertIn("native reasoning body", reasoning_text)
        self.assertIn("final answer text", answer_text)

    def test_final_reasoning_only_no_stream_reasoning(self):
        # Upstream did NOT stream reasoning but returned reasoning_content only at end.
        fake = FakeStreamingFlowithClient(
            chunks=["final answer text"],
            final_reasoning="end of turn reasoning summary",
        )
        events = self._run_responses_stream(fake)

        # After the fix this should show up as a reasoning item in completed output.
        reasoning_deltas = [d for e, d in events if e == "response.reasoning_summary_text.delta"]
        reasoning_text = "".join(x.get("delta", "") for x in reasoning_deltas)

        self.assertIn("end of turn reasoning summary", reasoning_text)


if __name__ == "__main__":
    unittest.main()
