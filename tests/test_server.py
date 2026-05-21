"""Integration tests for flowith_claude_proxy.server using FastAPI TestClient."""

import json
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from flowith_claude_proxy.server import app


@pytest.fixture
def client():
    return TestClient(app)


def _mock_flowith_response(content="Hello!", prompt_tokens=10, completion_tokens=5):
    return {
        "success": True,
        "content": content,
        "time_ms": 100.0,
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        "reasoning_content": "",
    }


# ── Health & root ──────────────────────────────────────────────

class TestHealth:
    def test_root(self, client):
        r = client.get("/")
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "flowith-claude-proxy"
        assert "POST /v1/messages" in data["endpoints"]

    def test_health(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


# ── Auth ───────────────────────────────────────────────────────

class TestAuth:
    def test_missing_key_returns_401(self, client):
        # The server may have FLOWITH_API_KEY from .env, so patch the resolver
        with patch("flowith_claude_proxy.server._resolve_api_key", return_value=None):
            r = client.post("/v1/messages", json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "hi"}],
            })
            assert r.status_code == 401

    def test_x_api_key_accepted(self, client):
        with patch("flowith_claude_proxy.server.FlowithClient") as MockClient:
            instance = MockClient.return_value
            instance.call_api.return_value = _mock_flowith_response()
            r = client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-api-key": "test-key"},
            )
            assert r.status_code == 200

    def test_authorization_bearer_accepted(self, client):
        with patch("flowith_claude_proxy.server.FlowithClient") as MockClient:
            instance = MockClient.return_value
            instance.call_api.return_value = _mock_flowith_response()
            r = client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"authorization": "Bearer test-key"},
            )
            assert r.status_code == 200


# ── Non-streaming ──────────────────────────────────────────────

class TestNonStreaming:
    def test_basic_response(self, client):
        with patch("flowith_claude_proxy.server.FlowithClient") as MockClient:
            instance = MockClient.return_value
            instance.call_api.return_value = _mock_flowith_response("Test reply")
            r = client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-api-key": "k"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["type"] == "message"
            assert data["content"][0]["text"] == "Test reply"
            assert data["stop_reason"] == "end_turn"

    def test_upstream_error_returns_502(self, client):
        with patch("flowith_claude_proxy.server.FlowithClient") as MockClient:
            instance = MockClient.return_value
            instance.call_api.return_value = {"success": False, "error": "boom"}
            r = client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 10,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-api-key": "k"},
            )
            assert r.status_code == 502


# ── Streaming ──────────────────────────────────────────────────

class TestStreaming:
    def test_streaming_events(self, client):
        with patch("flowith_claude_proxy.server.FlowithClient") as MockClient:
            instance = MockClient.return_value

            def fake_call_api(messages, **kwargs):
                if kwargs.get("stream"):
                    on_chunk = kwargs.get("on_chunk")
                    if on_chunk:
                        on_chunk("Hello ")
                        on_chunk("world")
                    return _mock_flowith_response("Hello world")
                return _mock_flowith_response()

            instance.call_api.side_effect = fake_call_api

            r = client.post(
                "/v1/messages",
                json={
                    "model": "claude-3-5-sonnet-20241022",
                    "max_tokens": 100,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"x-api-key": "k"},
            )
            assert r.status_code == 200
            body = r.text
            assert "event: message_start" in body
            assert "event: content_block_start" in body
            assert "event: content_block_delta" in body
            assert "event: content_block_stop" in body
            assert "event: message_stop" in body


# ── Input validation ──────────────────────────────────────────

class TestValidation:
    def test_invalid_json_returns_400(self, client):
        r = client.post(
            "/v1/messages",
            content=b"not json",
            headers={
                "content-type": "application/json",
                "x-api-key": "k",
            },
        )
        assert r.status_code == 400

    def test_empty_messages_returns_400(self, client):
        r = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 10,
                "messages": [],
            },
            headers={"x-api-key": "k"},
        )
        assert r.status_code == 400
