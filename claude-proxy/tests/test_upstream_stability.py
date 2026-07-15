import json
import time
import threading
import tempfile
import unittest
from pathlib import Path
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


class FakeHTTPErrorResponse:
    headers = {}

    def __init__(self, status_code, text="upstream error"):
        self.status_code = status_code
        self.text = text

    def json(self):
        raise AssertionError("HTTP error responses should not be parsed as success JSON")


class FakeStreamResponse:
    status_code = 200
    headers = {}
    text = ""

    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def iter_lines(self, decode_unicode=False):
        for line in self.lines:
            yield line

    def close(self):
        self.closed = True


class BlockingStreamResponse(FakeStreamResponse):
    def __init__(self, lines):
        super().__init__(lines)
        self.close_event = threading.Event()

    def iter_lines(self, decode_unicode=False):
        for line in self.lines:
            yield line
        self.close_event.wait(timeout=5)

    def close(self):
        self.closed = True
        self.close_event.set()


class IdleStreamResponse:
    """A dead stream: delivers zero content and blocks until the watchdog
    closes the socket, mirroring a hung upstream that never sends a byte."""

    status_code = 200
    headers = {}
    text = ""

    def __init__(self):
        self.closed = False
        self._closed_event = threading.Event()

    def iter_lines(self, decode_unicode=False):
        # Block as if waiting for bytes that never arrive; the watchdog aborts us
        # by calling close(), which unblocks the wait and ends the (empty) stream.
        self._closed_event.wait(timeout=5)
        return
        yield  # pragma: no cover - makes this a generator that yields nothing

    def close(self):
        self.closed = True
        self._closed_event.set()


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

    def test_nonretryable_http_4xx_fails_without_retry_delay(self):
        outcomes = [
            FakeHTTPErrorResponse(401, "invalid api key"),
            FakeHTTPErrorResponse(401, "invalid api key"),
            FakeHTTPErrorResponse(401, "invalid api key"),
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
                api_key="bad-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertFalse(result["success"])
        self.assertIn("HTTP 401", result["error"])
        self.assertEqual(len(sessions), 1)
        self.assertEqual(len(sessions[0].calls), 1)
        sleep_mock.assert_not_called()
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

    def test_debug_dump_redacts_secret_shaped_payload_and_response_headers(self):
        from proxy.upstream import _dump_intercept

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("proxy.upstream.DEBUG_DUMP", True),
            patch("proxy.upstream.DEBUG_DUMP_DIR", tmpdir),
        ):
            fake_openai_key = "sk-" + "live-1234567890abcdef"
            _dump_intercept(
                payload={
                    "api_key": "payload-secret",
                    "messages": [{"role": "user", "content": "keep prompt visible"}],
                    "nested": {"Authorization": "Bearer nested-secret"},
                },
                response_status=200,
                response_headers={
                    "Authorization": "Bearer response-secret",
                    "Set-Cookie": "session=response-secret",
                    "Content-Type": "application/json",
                },
                response_body='{"ok": true}',
                is_stream=False,
                upstream_model="claude-test",
            )

            dump_files = list(Path(tmpdir).glob("flowith_nonstream_*.json"))
            self.assertEqual(len(dump_files), 1)
            dump = json.loads(dump_files[0].read_text(encoding="utf-8"))

        dumped_text = json.dumps(dump, ensure_ascii=False)
        self.assertNotIn("payload-secret", dumped_text)
        self.assertNotIn("nested-secret", dumped_text)
        self.assertNotIn("response-secret", dumped_text)
        self.assertEqual(dump["request"]["payload"]["api_key"], "[REDACTED]")
        self.assertEqual(dump["request"]["payload"]["nested"]["Authorization"], "[REDACTED]")
        self.assertEqual(dump["response"]["headers"]["Authorization"], "[REDACTED]")
        self.assertEqual(dump["response"]["headers"]["Set-Cookie"], "[REDACTED]")
        self.assertEqual(dump["response"]["headers"]["Content-Type"], "application/json")
        self.assertEqual(dump["request"]["payload"]["messages"][0]["content"], "keep prompt visible")

    def test_debug_dump_redacts_secret_shaped_strings_inside_prompt_and_response_body(self):
        from proxy.upstream import _dump_intercept

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("proxy.upstream.DEBUG_DUMP", True),
            patch("proxy.upstream.DEBUG_DUMP_DIR", tmpdir),
        ):
            fake_openai_key = "sk-" + "live-1234567890abcdef"
            _dump_intercept(
                payload={
                    "messages": [
                        {
                            "role": "user",
                            "content": (
                                "debug this Authorization: Bearer prompt-secret-token "
                                f"and {fake_openai_key}"
                            ),
                        }
                    ],
                    "metadata": "token=metadata-secret",
                },
                response_status=200,
                response_headers={"Content-Type": "application/json"},
                response_body='{"content":"secret=response-secret and Bearer body-secret-token"}',
                is_stream=False,
                upstream_model="claude-test",
            )

            dump_files = list(Path(tmpdir).glob("flowith_nonstream_*.json"))
            self.assertEqual(len(dump_files), 1)
            dumped_text = dump_files[0].read_text(encoding="utf-8")

        self.assertNotIn("prompt-secret-token", dumped_text)
        self.assertNotIn(fake_openai_key, dumped_text)
        self.assertNotIn("metadata-secret", dumped_text)
        self.assertNotIn("response-secret", dumped_text)
        self.assertNotIn("body-secret-token", dumped_text)
        self.assertIn("Authorization: Bearer [REDACTED]", dumped_text)
        self.assertIn("sk-[REDACTED]", dumped_text)
        self.assertIn("token=[REDACTED]", dumped_text)
        self.assertIn("secret=[REDACTED]", dumped_text)

    def test_debug_dump_truncates_large_bodies_and_prunes_old_files(self):
        from proxy.upstream import _dump_intercept

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("proxy.upstream.DEBUG_DUMP", True),
            patch("proxy.upstream.DEBUG_DUMP_DIR", tmpdir),
            patch("proxy.upstream.DEBUG_DUMP_MAX_BYTES", 32),
            patch("proxy.upstream.DEBUG_DUMP_MAX_FILES", 2),
        ):
            old_a = Path(tmpdir) / "flowith_nonstream_20000101_000000_000001.json"
            old_b = Path(tmpdir) / "flowith_nonstream_20000101_000000_000002.json"
            old_a.write_text("{}", encoding="utf-8")
            old_b.write_text("{}", encoding="utf-8")

            _dump_intercept(
                payload={"messages": [{"role": "user", "content": "x" * 100}]},
                response_status=200,
                response_headers={"Content-Type": "application/json"},
                response_body="y" * 100,
                is_stream=False,
                upstream_model="claude-test",
            )

            dump_files = sorted(Path(tmpdir).glob("flowith_*.json"))
            self.assertEqual(len(dump_files), 2)
            self.assertNotIn(old_a, dump_files)
            newest = dump_files[-1]
            dump = json.loads(newest.read_text(encoding="utf-8"))

        self.assertLessEqual(len(dump["response"]["body"]), 80)
        self.assertIn("[truncated", dump["response"]["body"])
        self.assertLessEqual(len(dump["request"]["payload"]["messages"][0]["content"]), 80)
        self.assertIn("[truncated", dump["request"]["payload"]["messages"][0]["content"])

    def test_debug_dump_write_failure_is_swallowed(self):
        from proxy.upstream import _dump_intercept

        with (
            patch("proxy.upstream.DEBUG_DUMP", True),
            patch("proxy.upstream.Path.mkdir", side_effect=OSError("disk unavailable")),
        ):
            _dump_intercept(
                payload={"messages": [{"role": "user", "content": "ping"}]},
                response_status=200,
                response_headers={"Content-Type": "application/json"},
                response_body='{"ok": true}',
                is_stream=False,
                upstream_model="claude-test",
            )

    def test_debug_dump_concurrent_prune_does_not_raise(self):
        from proxy.upstream import _dump_intercept

        errors = []

        def _dump(i):
            try:
                _dump_intercept(
                    payload={"messages": [{"role": "user", "content": f"msg-{i}"}]},
                    response_status=200,
                    response_headers={"Content-Type": "application/json"},
                    response_body=f"body-{i}",
                    is_stream=False,
                    upstream_model="claude-test",
                )
            except Exception as e:  # pragma: no cover - failure path under test
                errors.append(e)

        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch("proxy.upstream.DEBUG_DUMP", True),
            patch("proxy.upstream.DEBUG_DUMP_DIR", tmpdir),
            patch("proxy.upstream.DEBUG_DUMP_MAX_FILES", 3),
        ):
            threads = [threading.Thread(target=_dump, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            dump_files = list(Path(tmpdir).glob("flowith_*.json"))

        self.assertEqual(errors, [])
        self.assertLessEqual(len(dump_files), 3)

    def test_http_error_response_does_not_expose_upstream_body_details(self):
        outcomes = [
            FakeHTTPErrorResponse(
                401,
                "invalid key for internal host db.internal.example with token secret-token",
            ),
        ]

        with (
            patch.object(requests, "Session", return_value=FakeSession(outcomes)),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 1),
            patch("proxy.upstream.FLOWITH_SSL_RETRY_EXTRA", 0),
            patch("proxy.upstream.time.sleep"),
        ):
            client = FlowithClient(
                api_key="bad-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertFalse(result["success"])
        self.assertIn("HTTP 401", result["error"])
        self.assertNotIn("db.internal", result["error"])
        self.assertNotIn("secret-token", result["error"])

    def test_upstream_semaphore_acquire_timeout_returns_failure(self):
        class NeverAcquireSemaphore:
            def acquire(self, timeout=None):
                self.timeout = timeout
                return False

            def release(self):
                raise AssertionError("release should not be called if acquire failed")

        semaphore = NeverAcquireSemaphore()

        with (
            patch("proxy.upstream._UPSTREAM_SEMAPHORE", semaphore),
            patch("proxy.upstream.FLOWITH_SEMAPHORE_TIMEOUT", 0.01),
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api([{"role": "user", "content": "ping"}], max_retries=1)

        self.assertFalse(result["success"])
        self.assertEqual(semaphore.timeout, 0.01)
        self.assertIn("concurrency limit", result["error"])


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


    def test_streaming_empty_stop_response_is_retried_before_returning_success(self):
        empty_stream = FakeStreamResponse([
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        good_stream = FakeStreamResponse([
            'data: {"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":null}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        outcomes = [empty_stream, good_stream]
        sessions = []
        chunks = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 2),
            patch("proxy.upstream.time.sleep") as sleep_mock,
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=True,
                on_chunk=chunks.append,
                max_retries=1,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(chunks, ["pong"])
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sleep_mock.call_count, 1)

    def test_nonstreaming_empty_response_is_retried_before_returning_success(self):
        class EmptyResponse(FakeResponse):
            text = '{"model":"ok","choices":[{"message":{"content":""},"finish_reason":"stop"}],"usage":{}}'

            def json(self):
                return {
                    "model": "ok",
                    "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
                    "usage": {},
                }

        outcomes = [EmptyResponse(), FakeResponse()]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 2),
            patch("proxy.upstream.time.sleep") as sleep_mock,
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=False,
                max_retries=1,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sleep_mock.call_count, 1)


    def test_streaming_error_chunk_returns_failure_not_success(self):
        error_stream = FakeStreamResponse([
            'data: {"error":{"message":"provider overloaded"}}',
            "data: [DONE]",
        ])
        sessions = []

        def make_session():
            session = FakeSession([error_stream])
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
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=True,
                max_retries=1,
            )

        self.assertFalse(result["success"])
        self.assertIn("provider overloaded", result["error"])

    def test_streaming_partial_stream_without_terminal_event_is_failure(self):
        partial_stream = FakeStreamResponse([
            'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}',
        ])
        chunks = []

        with (
            patch.object(requests, "Session", return_value=FakeSession([partial_stream])),
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
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=True,
                on_chunk=chunks.append,
                max_retries=1,
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["content"], "partial")
        self.assertIn("ended before completion", result["error"])
        self.assertEqual(chunks, ["partial"])

    def test_streaming_debug_body_is_not_accumulated_when_debug_dump_is_disabled(self):
        stream = FakeStreamResponse([
            'data: {"choices":[{"delta":{"content":"pong"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        captured = {}

        def capture_dump(**kwargs):
            captured.update(kwargs)

        with (
            patch.object(requests, "Session", return_value=FakeSession([stream])),
            patch("proxy.upstream.DEBUG_DUMP", False),
            patch("proxy.upstream._dump_intercept", side_effect=capture_dump),
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
            result = client.call_api([{"role": "user", "content": "ping"}], stream=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(captured["response_body"], "")

    def test_streaming_debug_body_is_bounded_before_dumping(self):
        long_piece = "x" * 200
        stream = FakeStreamResponse([
            f'data: {{"choices":[{{"delta":{{"content":"{long_piece}"}},'
            f'"finish_reason":null}}]}}',
            "data: [DONE]",
        ])
        captured = {}

        def capture_dump(**kwargs):
            captured.update(kwargs)

        with (
            patch.object(requests, "Session", return_value=FakeSession([stream])),
            patch("proxy.upstream.DEBUG_DUMP", True),
            patch("proxy.upstream.DEBUG_DUMP_MAX_BYTES", 32),
            patch("proxy.upstream._dump_intercept", side_effect=capture_dump),
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
            result = client.call_api([{"role": "user", "content": "ping"}], stream=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], long_piece)
        self.assertLessEqual(len(captured["response_body"].encode("utf-8")), 96)
        self.assertIn("[truncated", captured["response_body"])

    def test_streaming_success_is_not_delayed_by_slow_debug_dump(self):
        stream = FakeStreamResponse([
            'data: {"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":null}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])

        def slow_dump(**kwargs):
            threading.Event().wait(0.2)

        with (
            patch.object(requests, "Session", return_value=FakeSession([stream])),
            patch("proxy.upstream.DEBUG_DUMP", True),
            patch("proxy.upstream._dump_intercept", side_effect=slow_dump),
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
            start = time.perf_counter()
            result = client.call_api([{"role": "user", "content": "ping"}], stream=True)
            elapsed = time.perf_counter() - start

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertLess(elapsed, 0.1)

    def test_streaming_success_closes_upstream_response(self):
        stream = FakeStreamResponse([
            'data: {"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":null}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        chunks = []

        with (
            patch.object(requests, "Session", return_value=FakeSession([stream])),
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
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=True,
                on_chunk=chunks.append,
                max_retries=1,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(chunks, ["pong"])
        self.assertTrue(stream.closed)
    def test_streaming_cancel_event_closes_upstream_response(self):
        stream = BlockingStreamResponse([
            'data: {"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":null}]}',
        ])
        sessions = []
        cancel_event = threading.Event()
        chunk_event = threading.Event()
        chunks = []
        result_holder = {}

        def make_session():
            session = FakeSession([stream])
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

            def call_stream():
                result_holder["result"] = client.call_api(
                    [{"role": "user", "content": "ping"}],
                    stream=True,
                    on_chunk=lambda chunk: (chunks.append(chunk), chunk_event.set()),
                    max_retries=1,
                    cancel_event=cancel_event,
                )

            thread = threading.Thread(target=call_stream)
            thread.start()
            self.assertTrue(chunk_event.wait(timeout=2))
            self.assertEqual(chunks, ["pong"])
            cancel_event.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertTrue(stream.closed)
        self.assertFalse(result_holder["result"]["success"])
        self.assertIn("cancelled", result_holder["result"]["error"])


    def test_streaming_idle_stream_is_aborted_by_watchdog_and_retried_as_empty(self):
        # First stream hangs with no content -> watchdog aborts on the idle
        # timeout. That must be treated as an empty stream (fast bounded retry),
        # not a generic error, and the retry should then succeed.
        idle_stream = IdleStreamResponse()
        good_stream = FakeStreamResponse([
            'data: {"choices":[{"index":0,"delta":{"content":"pong"},"finish_reason":null}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        outcomes = [idle_stream, good_stream]
        sessions = []
        chunks = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_STREAM_IDLE_TIMEOUT", 0.2),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 2),
            patch("proxy.upstream.time.sleep") as sleep_mock,
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=True,
                on_chunk=chunks.append,
                max_retries=1,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "pong")
        self.assertEqual(chunks, ["pong"])
        self.assertTrue(idle_stream.closed)
        self.assertEqual(len(sessions), 2)
        # Routed through the empty-stream path: exactly one fast empty-retry delay,
        # not the generic exponential-backoff schedule.
        self.assertEqual(sleep_mock.call_count, 1)

    def test_idle_timeout_does_not_repeat_the_generic_retry_budget(self):
        idle_stream = IdleStreamResponse()
        good_stream = FakeStreamResponse([
            'data: {"choices":[{"index":0,"delta":{"content":"late"},"finish_reason":null}]}',
            'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ])
        outcomes = [idle_stream, good_stream]
        sessions = []

        def make_session():
            session = FakeSession(outcomes)
            sessions.append(session)
            return session

        with (
            patch.object(requests, "Session", side_effect=make_session),
            patch("proxy.upstream.FLOWITH_STREAM_IDLE_TIMEOUT", 0.2),
            patch("proxy.upstream.FLOWITH_RETRY_TOTAL", 2),
            patch("proxy.upstream.FLOWITH_EMPTY_RETRY_WINDOW", 0),
            patch("proxy.upstream.FLOWITH_EMPTY_RETRY_TOTAL", 0),
            patch("proxy.upstream.time.sleep"),
        ):
            client = FlowithClient(
                api_key="test-key",
                model="claude-test",
                base_url="https://edge.flowith.io/external/use/llm",
                ssl_verify=True,
            )
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=True,
                max_retries=1,
            )

        self.assertFalse(result["success"])
        self.assertTrue(result["empty_response"])
        self.assertEqual(sum(len(session.calls) for session in sessions), 1)

    def test_streaming_healthy_stream_is_not_killed_by_idle_watchdog(self):
        # A stream that keeps delivering content must never trip the watchdog,
        # even with a very short idle timeout, because each delta refreshes the
        # activity clock.
        class SlowButAliveStream:
            status_code = 200
            headers = {}
            text = ""

            def __init__(self, pieces):
                self.pieces = pieces
                self.closed = False

            def iter_lines(self, decode_unicode=False):
                for piece in self.pieces:
                    time.sleep(0.05)
                    yield (
                        'data: {"choices":[{"index":0,"delta":'
                        f'{{"content":"{piece}"}},"finish_reason":null}}]}}'
                    )
                yield 'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}'
                yield "data: [DONE]"

            def close(self):
                self.closed = True

        alive_stream = SlowButAliveStream(["a", "b", "c", "d"])
        chunks = []

        with (
            patch.object(requests, "Session", return_value=FakeSession([alive_stream])),
            patch("proxy.upstream.FLOWITH_STREAM_IDLE_TIMEOUT", 0.2),
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
            result = client.call_api(
                [{"role": "user", "content": "ping"}],
                stream=True,
                on_chunk=chunks.append,
                max_retries=1,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "abcd")
        self.assertEqual(chunks, ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()
