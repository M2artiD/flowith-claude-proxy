import unittest
from unittest.mock import patch

import requests
from requests.exceptions import ConnectTimeout, SSLError

from proxy.upstream import FlowithClient


class FakeResponse:
    status_code = 200
    headers = {}
    text = '{"model":"ok","choices":[{"message":{"content":"pong"},"finish_reason":"stop"}],"usage":{}}'

    def json(self):
        return {
            "model": "ok",
            "choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}],
            "usage": {},
        }


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.headers = {}
        self.proxies = {}
        self.trust_env = False
        self.verify = True
        self.closed = False
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def mount(self, prefix, adapter):
        pass

    def close(self):
        self.closed = True


class UpstreamStabilityTests(unittest.TestCase):
    def test_ssl_eof_is_retried_with_new_session_without_disabling_verify(self):
        outcomes = [SSLError("EOF occurred in violation of protocol"), FakeResponse()]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with patch.object(requests, "Session", side_effect=make_session), patch("proxy.upstream.time.sleep"):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=2)

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertGreaterEqual(len(sessions), 2)
        self.assertTrue(sessions[0].closed)
        self.assertTrue(all(session.verify is True for session in sessions))

    def test_call_api_uses_configured_retry_floor_even_when_caller_asks_once(self):
        outcomes = [
            ConnectTimeout("connect timed out"),
            ConnectTimeout("connect timed out again"),
            FakeResponse(),
        ]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 3),
            patch("proxy.upstream.time.sleep") as sleep_mock,
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_request_uses_fast_connect_timeout_and_long_read_timeout(self):
        outcomes = [FakeResponse()]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_CONNECT_TIMEOUT", 30),
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                timeout=300,
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertTrue(result["success"])
        self.assertEqual(sessions[0].calls[0][1]["timeout"], (30, 300))

    def test_retry_delay_is_capped_for_efficient_reconnects(self):
        with (
            patch("proxy.upstream.FLOWITH_RETRY_BACKOFF", 10),
            patch("proxy.upstream.FLOWITH_RETRY_JITTER", 0),
            patch("proxy.upstream.FLOWITH_RETRY_MAX_DELAY", 8),
        ):
            from proxy.upstream import _retry_delay

            self.assertEqual(_retry_delay(4), 8)

    def test_call_api_uses_configured_retry_floor_even_when_caller_asks_once(self):
        outcomes = [
            ConnectTimeout("connect timed out"),
            ConnectTimeout("connect timed out again"),
            FakeResponse(),
        ]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 3),
            patch("proxy.upstream.time.sleep") as sleep_mock,
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(len(sessions), 3)
        self.assertEqual(sleep_mock.call_count, 2)

    def test_request_uses_fast_connect_timeout_and_long_read_timeout(self):
        outcomes = [FakeResponse()]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_CONNECT_TIMEOUT", 30),
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                timeout=300,
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertTrue(result["success"])
        self.assertEqual(sessions[0].calls[0][1]["timeout"], (30, 300))

    def test_retry_delay_is_capped_for_efficient_reconnects(self):
        with (
            patch("proxy.upstream.FLOWITH_RETRY_BACKOFF", 10),
            patch("proxy.upstream.FLOWITH_RETRY_JITTER", 0),
            patch("proxy.upstream.FLOWITH_RETRY_MAX_DELAY", 8),
        ):
            from proxy.upstream import _retry_delay

            self.assertEqual(_retry_delay(4), 8)


    def test_concurrent_calls_use_isolated_sessions(self):
        import threading

        sessions = []
        sessions_lock = threading.Lock()
        barrier = threading.Barrier(2)

        class BlockingSession(FakeSession):
            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                barrier.wait(timeout=2)
                return FakeResponse()

        def make_session():
            session = BlockingSession([FakeResponse()])
            with sessions_lock:
                sessions.append(session)
            return session

        with patch.object(requests, "Session", side_effect=make_session):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            results = []

            def worker():
                results.append(client.call_api([{"role": "user", "content": "ping"}], max_retries=1))

            threads = [threading.Thread(target=worker), threading.Thread(target=worker)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result["success"] for result in results))
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sum(len(session.calls) for session in sessions), 2)

    def test_keepalive_is_disabled_by_default_to_avoid_stale_tls_reuse(self):
        sessions = []

        def make_session():
            session = FakeSession([FakeResponse()])
            sessions.append(session)
            return session

        with patch.object(requests, "Session", side_effect=make_session):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertTrue(result["success"])
        self.assertEqual(sessions[0].headers["Connection"], "close")

    def test_ssl_eof_uses_extra_attempt_budget_for_transient_handshake_failures(self):
        outcomes = [
            SSLError("EOF occurred in violation of protocol"),
            SSLError("EOF occurred in violation of protocol"),
            SSLError("EOF occurred in violation of protocol"),
            FakeResponse(),
        ]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 2),
            patch("proxy.upstream.FLOWITH_SSL_RETRY_EXTRA", 2),
            patch("proxy.upstream.time.sleep"),
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(len(sessions), 4)

    def test_ssl_eof_reconnects_ten_times_by_default(self):
        outcomes = [SSLError("EOF occurred in violation of protocol") for _ in range(10)]
        outcomes.append(FakeResponse())
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 1),
            patch("proxy.upstream.time.sleep"),
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(len(sessions), 11)
        self.assertTrue(all(session.closed for session in sessions[:-1]))

    def test_ssl_error_message_keeps_certificate_verification_enabled_by_default(self):
        outcomes = [SSLError("EOF occurred in violation of protocol")]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 1),
            patch("proxy.upstream.FLOWITH_SSL_RETRY_EXTRA", 0),
            patch("proxy.upstream.time.sleep"),
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertFalse(result["success"])
        self.assertIn("Upstream SSL error", result["error"])
        self.assertNotIn("FLOWITH_SSL_VERIFY=false", result["error"])


if __name__ == "__main__":
    unittest.main()
