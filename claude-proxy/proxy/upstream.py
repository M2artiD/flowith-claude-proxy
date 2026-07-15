"""Minimal HTTP client for the Flowith LLM endpoint."""

from __future__ import annotations

import json
import os
import random
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, SSLError
from urllib3.util.retry import Retry

from .config import (
    DEBUG_DUMP,
    DEBUG_DUMP_DIR,
    DEBUG_DUMP_MAX_BYTES,
    DEBUG_DUMP_MAX_FILES,
    FLOWITH_CONNECT_TIMEOUT,
    FLOWITH_DISABLE_KEEPALIVE,
    FLOWITH_EMPTY_RETRY_DELAY,
    FLOWITH_EMPTY_RETRY_DELAY_MAX,
    FLOWITH_EMPTY_RETRY_TOTAL,
    FLOWITH_EMPTY_RETRY_WINDOW,
    FLOWITH_FABLE_STREAM_IDLE_TIMEOUT,
    FLOWITH_MAX_CONCURRENCY,
    FLOWITH_POOL_MAXSIZE,
    FLOWITH_RETRY_BACKOFF,
    FLOWITH_RETRY_JITTER,
    FLOWITH_RETRY_MAX_DELAY,
    FLOWITH_RETRY_TOTAL,
    FLOWITH_SEMAPHORE_TIMEOUT,
    FLOWITH_SSL_RETRY_EXTRA,
    FLOWITH_STREAM_IDLE_TIMEOUT,
)


_REDACTED = "[REDACTED]"
_SECRET_FIELD_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "set-cookie",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
)
_SECRET_STRING_PATTERNS = (
    (
        re.compile(r"(?i)\b(authorization\s*:\s*bearer\s+)([^\s\"'\\,}]+)"),
        r"\1" + _REDACTED,
    ),
    (
        re.compile(r"(?i)\b(bearer\s+)([^\s\"'\\,}]+)"),
        r"\1" + _REDACTED,
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9._-]{6,}"),
        "sk-" + _REDACTED,
    ),
    (
        re.compile(
            r"(?i)\b(token|secret|api[_-]?key|password|passwd|credential)\s*[:=]\s*"
            r"([^\s\"'\\,}&]+)"
        ),
        r"\1=" + _REDACTED,
    ),
)


def _is_secret_field(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker.replace("-", "_") in normalized for marker in _SECRET_FIELD_MARKERS)


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _REDACTED if _is_secret_field(key) else _redact_secrets(nested)
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_secrets(item) for item in value)
    if isinstance(value, str):
        redacted = value
        for pattern, replacement in _SECRET_STRING_PATTERNS:
            redacted = pattern.sub(replacement, redacted)
        return redacted
    return value




def _truncate_debug_value(value: Any) -> Any:
    if DEBUG_DUMP_MAX_BYTES <= 0:
        return value
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        if len(encoded) <= DEBUG_DUMP_MAX_BYTES:
            return value
        kept = encoded[:DEBUG_DUMP_MAX_BYTES].decode("utf-8", errors="ignore")
        omitted = len(encoded) - DEBUG_DUMP_MAX_BYTES
        return f"{kept}... [truncated {omitted} bytes]"
    if isinstance(value, dict):
        return {key: _truncate_debug_value(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_truncate_debug_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_truncate_debug_value(item) for item in value)
    return value


_DEBUG_DUMP_LOCK = threading.Lock()


def _prune_debug_dumps(dump_dir: Path) -> None:
    if DEBUG_DUMP_MAX_FILES <= 0:
        return
    entries: list[tuple[int, str, Path]] = []
    for p in dump_dir.glob("flowith_*.json"):
        try:
            entries.append((p.stat().st_mtime_ns, p.name, p))
        except OSError:
            # Concurrent dump/prune already removed it; safe to skip.
            continue
    dump_files = [p for _, _, p in sorted(entries, key=lambda e: (e[0], e[1]))]
    for stale in dump_files[:-DEBUG_DUMP_MAX_FILES]:
        try:
            stale.unlink()
        except OSError:
            pass


def _dump_intercept(
    payload: dict[str, Any],
    response_status: int,
    response_headers: dict[str, str],
    response_body: str,
    is_stream: bool,
    upstream_model: str | None,
) -> None:
    if not DEBUG_DUMP:
        return
    try:
        dump_dir = Path(DEBUG_DUMP_DIR)
        dump_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        stream_label = "stream" if is_stream else "nonstream"
        dump_path = dump_dir / f"flowith_{stream_label}_{ts}.json"
        dump = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "request": {
                "method": "POST",
                "url": os.environ.get("FLOWITH_BASE_URL", "https://edge.flowith.io/external/use/llm"),
                "payload": _truncate_debug_value(_redact_secrets(payload)),
            },
            "response": {
                "status": response_status,
                "headers": _truncate_debug_value(_redact_secrets(dict(response_headers))),
                "body": _truncate_debug_value(_redact_secrets(response_body)),
            },
            "upstream_model": upstream_model,
        }
        with open(dump_path, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=2)
        with _DEBUG_DUMP_LOCK:
            _prune_debug_dumps(dump_dir)
        print(f"[intercept] Dumped upstream {stream_label} call -> {dump_path}", flush=True)
    except Exception as e:
        print(f"[intercept] Failed to dump upstream call: {e}", flush=True)


def _dump_intercept_async(**kwargs: Any) -> None:
    if not DEBUG_DUMP:
        _dump_intercept(**kwargs)
        return

    def _run() -> None:
        _dump_intercept(**kwargs)

    threading.Thread(target=_run, daemon=True).start()


_RETRY_STRATEGY = Retry(
    total=FLOWITH_RETRY_TOTAL,
    connect=FLOWITH_RETRY_TOTAL,
    read=FLOWITH_RETRY_TOTAL,
    backoff_factor=FLOWITH_RETRY_BACKOFF,
    backoff_jitter=FLOWITH_RETRY_JITTER,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=None,
    raise_on_status=False,
)

_UPSTREAM_SEMAPHORE = (
    threading.BoundedSemaphore(FLOWITH_MAX_CONCURRENCY)
    if FLOWITH_MAX_CONCURRENCY > 0
    else None
)


def _retry_delay(attempt: int) -> float:
    delay = FLOWITH_RETRY_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, FLOWITH_RETRY_JITTER)
    return min(delay, FLOWITH_RETRY_MAX_DELAY)


def _request_timeout(read_timeout: int | float) -> tuple[float, int | float]:
    connect_timeout = max(1.0, min(float(FLOWITH_CONNECT_TIMEOUT), float(read_timeout)))
    return (connect_timeout, read_timeout)


def _stream_result_delivered_any(result: dict[str, Any]) -> bool:
    return bool(
        result.get("content")
        or result.get("reasoning_content")
        or result.get("tool_calls")
    )


def _stream_idle_timeout(model: str | None) -> float:
    if str(model or "").strip().lower() == "claude-fable-5":
        return FLOWITH_FABLE_STREAM_IDLE_TIMEOUT
    return FLOWITH_STREAM_IDLE_TIMEOUT


def _pick_reasoning_field(obj: dict[str, Any]) -> str:
    """Return the first non-empty reasoning-like field from an upstream delta/message.

    Flowith upstream varies between providers; different backends label the chain-of-thought
    stream differently. We accept any of the common spellings and normalise to a single string.
    """
    if not isinstance(obj, dict):
        return ""
    for key in (
        "reasoning_content",
        "reasoning",
        "thinking",
        "thought",
        "thoughts",
        "reasoning_text",
        "cot",
    ):
        val = obj.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            # Anthropic-style {"type": "thinking", "thinking": "..."}
            for sub in ("thinking", "text", "content"):
                inner = val.get(sub)
                if isinstance(inner, str) and inner:
                    return inner
        if isinstance(val, list):
            parts: list[str] = []
            for item in val:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    for sub in ("thinking", "text", "content", "reasoning"):
                        inner = item.get(sub)
                        if isinstance(inner, str):
                            parts.append(inner)
                            break
            joined = "".join(parts)
            if joined:
                return joined
    return ""


def _extract_content_and_thinking(content: Any) -> tuple[str, str]:
    """Normalise a message/delta ``content`` field to (text, thinking).

    Handles three shapes:

    * plain string -> (text, "")
    * Anthropic-style block list ``[{"type": "thinking", ...}, {"type": "text", ...}]``
      -> concatenated text + concatenated thinking
    * dict with ``text`` / ``content`` keys -> best-effort extraction
    * ``None`` or unknown -> ("", "")
    """
    if content is None:
        return "", ""
    if isinstance(content, str):
        return content, ""
    if isinstance(content, list):
        texts: list[str] = []
        thinks: list[str] = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type") or ""
            if btype in ("thinking", "reasoning", "thought"):
                for sub in ("thinking", "reasoning", "text", "content"):
                    val = block.get(sub)
                    if isinstance(val, str) and val:
                        thinks.append(val)
                        break
                continue
            if btype in ("text", "output_text", ""):
                for sub in ("text", "content"):
                    val = block.get(sub)
                    if isinstance(val, str) and val:
                        texts.append(val)
                        break
        return "".join(texts), "".join(thinks)
    if isinstance(content, dict):
        for sub in ("text", "content"):
            val = content.get(sub)
            if isinstance(val, str):
                return val, ""
        return "", ""
    return str(content), ""


class FlowithClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str,
        timeout: int = 120,
        thinking: bool = False,
        ssl_verify: bool = True,
        proxies: dict[str, str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url
        self.timeout = timeout
        self.thinking = thinking
        self.ssl_verify = ssl_verify
        self.proxies = proxies
        self._thread_local = threading.local()
        self._session: requests.Session | None = None

    def _make_session(self) -> requests.Session:
        session = requests.Session()
        session.proxies = self.proxies or {}
        session.headers.update({"Authorization": f"Bearer {self.api_key}"})
        if FLOWITH_DISABLE_KEEPALIVE:
            session.headers.update({"Connection": "close"})
        session.trust_env = False
        session.verify = self.ssl_verify
        if not self.ssl_verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        adapter = HTTPAdapter(
            # Keep retries in call_api so SSL EOFs rebuild the Session between attempts.
            max_retries=0,
            pool_connections=FLOWITH_POOL_MAXSIZE,
            pool_maxsize=FLOWITH_POOL_MAXSIZE,
            pool_block=True,
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        return session

    def _get_session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self._make_session()
            self._thread_local.session = session
            if threading.current_thread() is threading.main_thread():
                self._session = session
        return session

    def _reset_session(self) -> None:
        session = getattr(self._thread_local, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass
        session = self._make_session()
        self._thread_local.session = session
        if threading.current_thread() is threading.main_thread():
            self._session = session

    def call_api(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_retries: int = 1,
        stream: bool = False,
        on_chunk: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
        on_tool_call: Callable[[dict[str, Any]], None] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        thinking: bool | None = None,
        thinking_budget_tokens: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        top_p: float | None = None,
        stop_sequences: list[str] | str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        use_model = model or self.model
        last_error: Exception | None = None

        base_attempts = max(1, max_retries, FLOWITH_RETRY_TOTAL)
        attempt = 0
        ssl_extra_used = 0
        # Empty upstream streams cluster in time (measured: long runs of empties
        # punctuated by brief OK windows), so we retry ACROSS a wall-clock window
        # long enough to outlast a bad window rather than a fixed count of fast
        # retries that could all land inside it. empty_window_start marks the first
        # empty; we keep extending the loop until FLOWITH_EMPTY_RETRY_WINDOW elapses
        # (or the optional hard count cap trips), backing off from a fast flat delay
        # to a longer capped one so early isolated empties retry cheaply while a
        # clustered outage gets ridden out with spaced-out attempts.
        empty_extra_used = 0
        empty_window_start: float | None = None
        last_was_empty_stream = False
        delivered_any = {"v": False}
        last_stream_result: dict[str, Any] | None = None

        if stream:
            _orig_on_chunk = on_chunk
            _orig_on_reasoning = on_reasoning
            _orig_on_tool_call = on_tool_call

            def _wrap_chunk(text: str) -> None:
                if text:
                    delivered_any["v"] = True
                if _orig_on_chunk is not None:
                    _orig_on_chunk(text)

            def _wrap_reasoning(text: str) -> None:
                if text:
                    delivered_any["v"] = True
                if _orig_on_reasoning is not None:
                    _orig_on_reasoning(text)

            def _wrap_tool_call(tc: dict[str, Any]) -> None:
                delivered_any["v"] = True
                if _orig_on_tool_call is not None:
                    _orig_on_tool_call(tc)

            on_chunk = _wrap_chunk
            on_reasoning = _wrap_reasoning
            on_tool_call = _wrap_tool_call

        while attempt < base_attempts + ssl_extra_used + empty_extra_used:
            attempt += 1
            acquired = False
            retryable_ssl_error = False
            last_was_empty_stream = False
            try:
                if _UPSTREAM_SEMAPHORE is not None:
                    acquired = _UPSTREAM_SEMAPHORE.acquire(timeout=FLOWITH_SEMAPHORE_TIMEOUT)
                    if not acquired:
                        last_error = Exception("Upstream concurrency limit reached; timed out waiting for an available slot")
                        break

                start = time.time()
                use_thinking = thinking if thinking is not None else self.thinking

                payload: dict[str, Any] = {
                    "models": [use_model],
                    "messages": messages,
                    "stream": stream,
                    "thinking": use_thinking,
                }
                if tools:
                    payload["tools"] = tools
                if tool_choice is not None:
                    payload["tool_choice"] = tool_choice
                if thinking_budget_tokens is not None:
                    payload["thinking_budget_tokens"] = thinking_budget_tokens
                if max_tokens is not None:
                    payload["max_tokens"] = max_tokens
                if temperature is not None:
                    payload["temperature"] = temperature
                if top_p is not None:
                    payload["top_p"] = top_p
                if stop_sequences:
                    payload["stop"] = stop_sequences

                response = self._get_session().post(
                    self.base_url,
                    json=payload,
                    timeout=_request_timeout(self.timeout),
                    stream=stream,
                )
                elapsed_ms = (time.time() - start) * 1000

                if response.status_code != 200:
                    _dump_intercept(
                        payload=payload,
                        response_status=response.status_code,
                        response_headers=dict(response.headers),
                        response_body=response.text[:2000],
                        is_stream=stream,
                        upstream_model=None,
                    )
                    last_error = Exception(f"HTTP {response.status_code}: upstream returned an error response")
                    if response.status_code in {429, 500, 502, 503, 504}:
                        self._reset_session()
                    else:
                        break
                else:
                    if stream:
                        result = self._parse_stream(
                            response, elapsed_ms, payload,
                            on_chunk, on_reasoning, on_tool_call,
                            cancel_event=cancel_event,
                            idle_timeout=_stream_idle_timeout(use_model),
                        )
                        if result.get("success") and _stream_result_delivered_any(result):
                            return result
                        last_stream_result = result
                        if result.get("idle_timeout"):
                            # Watchdog aborted a stream that delivered zero content
                            # for the whole idle window. That's a dead stream, the
                            # same failure mode as an empty SSE frame -- route it to
                            # the empty-retry path (bounded window) instead of the
                            # generic error path (which would burn N x idle_timeout).
                            last_error = Exception(str(result["error"]))
                            last_was_empty_stream = True
                            # An idle timeout already spent the expensive
                            # first-byte budget. Do not repeat the generic
                            # six-attempt loop before entering the bounded
                            # empty-response retry path.
                            base_attempts = min(base_attempts, attempt)
                        elif result.get("error"):
                            last_error = Exception(str(result["error"]))
                        else:
                            # success:True but nothing delivered -> empty SSE frame
                            # (delta:{}+finish_reason:stop). Provider dice-roll; retry
                            # fast on the dedicated empty budget.
                            last_error = Exception("Upstream stream ended without content")
                            last_was_empty_stream = True
                        self._reset_session()
                    else:
                        resp_body = response.text
                        result = response.json()
                        upstream_model = result.get("model")

                        _dump_intercept(
                            payload=payload,
                            response_status=response.status_code,
                            response_headers=dict(response.headers),
                            response_body=resp_body,
                            is_stream=False,
                            upstream_model=upstream_model,
                        )

                        if result.get("choices"):
                            msg = result["choices"][0]["message"]
                            tool_calls = msg.get("tool_calls")
                            content_val, thinking_from_blocks = _extract_content_and_thinking(msg.get("content"))
                            reasoning_text = _pick_reasoning_field(msg) or thinking_from_blocks
                            if content_val or reasoning_text or tool_calls:
                                return {
                                    "success": True,
                                    "content": content_val,
                                    "time_ms": elapsed_ms,
                                    "usage": result.get("usage", {}) or {},
                                    "reasoning_content": reasoning_text,
                                    "tool_calls": tool_calls,
                                    "finish_reason": result["choices"][0].get("finish_reason"),
                                    "upstream_model": upstream_model,
                                }
                            last_error = Exception("Upstream response ended without content")
                            last_was_empty_stream = True
                            self._reset_session()
                        else:
                            last_error = Exception("Upstream response has no choices")

            except requests.exceptions.Timeout:
                self._reset_session()
                last_error = Exception("Upstream request timed out")
            except SSLError as e:
                self._reset_session()
                retryable_ssl_error = True
                last_error = Exception(
                    "Upstream SSL error after retries; retried with fresh TLS sessions. "
                    "If it persists, check local proxy, firewall, CA certificate settings, "
                    "or tune FLOWITH_MAX_CONCURRENCY for your network. "
                    f"Original: {e}"
                )
            except RequestException as e:
                self._reset_session()
                last_error = Exception(f"Upstream request failed: {e}")
            except OSError as e:
                self._reset_session()
                last_error = Exception(f"Upstream connection error: {e}")
            except Exception as e:
                last_error = e
            finally:
                if acquired and _UPSTREAM_SEMAPHORE is not None:
                    _UPSTREAM_SEMAPHORE.release()

            if (
                retryable_ssl_error
                and attempt >= base_attempts + ssl_extra_used + empty_extra_used
                and ssl_extra_used < FLOWITH_SSL_RETRY_EXTRA
            ):
                ssl_extra_used += 1

            # Empty upstream streams cluster in time, so keep extending the loop as
            # long as we're still inside the empty-retry window (and under the
            # optional hard count cap) rather than a fixed budget. This lets a run
            # of empties get ridden out until the upstream recovers instead of
            # exhausting the allowance and forcing a silent no-content turn.
            if last_was_empty_stream and attempt >= base_attempts + ssl_extra_used + empty_extra_used:
                now = time.time()
                if empty_window_start is None:
                    empty_window_start = now
                within_window = (now - empty_window_start) < FLOWITH_EMPTY_RETRY_WINDOW
                under_cap = FLOWITH_EMPTY_RETRY_TOTAL <= 0 or empty_extra_used < FLOWITH_EMPTY_RETRY_TOTAL
                if within_window and under_cap:
                    empty_extra_used += 1

            if stream and delivered_any["v"]:
                # Bytes already sent to the client; re-issuing the upstream call
                # would produce a duplicated response stream. Return the parser's
                # failure payload so callers can preserve any partial content.
                if last_stream_result is not None:
                    return last_stream_result
                break

            if attempt < base_attempts + ssl_extra_used + empty_extra_used:
                # Empty streams aren't congestion. They cluster, so back off from a
                # fast flat delay (cheap for isolated empties) toward a longer
                # capped delay as the window wears on, spacing out attempts to ride
                # out a clustered outage instead of hammering inside a dead window.
                if last_was_empty_stream:
                    if empty_window_start is not None:
                        elapsed = time.time() - empty_window_start
                        frac = min(1.0, elapsed / FLOWITH_EMPTY_RETRY_WINDOW) if FLOWITH_EMPTY_RETRY_WINDOW > 0 else 1.0
                    else:
                        frac = 0.0
                    delay = FLOWITH_EMPTY_RETRY_DELAY + frac * (FLOWITH_EMPTY_RETRY_DELAY_MAX - FLOWITH_EMPTY_RETRY_DELAY)
                    time.sleep(max(0.0, delay) + random.uniform(0, FLOWITH_RETRY_JITTER))
                else:
                    time.sleep(_retry_delay(attempt))

        # If we exhausted the empty-retry window without ever getting content,
        # surface an explicit, actionable error (rather than a bare
        # "ended without content") so the caller can re-issue the request instead
        # of silently emitting an empty turn.
        if last_was_empty_stream and empty_window_start is not None:
            waited = time.time() - empty_window_start
            return {
                "success": False,
                "empty_response": True,
                "error": (
                    f"Upstream returned no content for {waited:.0f}s and exhausted "
                    "the bounded empty-response retry budget. Fable commonly does "
                    "this when the conversation is too large; compact the context, "
                    "start a new task, or retry the request."
                ),
            }
        return {"success": False, "error": str(last_error) if last_error else "unknown error"}

    def _parse_stream(
        self,
        response: requests.Response,
        elapsed_ms: float,
        payload: dict[str, Any],
        on_chunk: Callable[[str], None] | None,
        on_reasoning: Callable[[str], None] | None,
        on_tool_call: Callable[[dict[str, Any]], None] | None,
        cancel_event: threading.Event | None = None,
        idle_timeout: float | None = None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] = {}
        tool_call_accum: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        upstream_model: str | None = None
        debug_body_parts: list[str] = []
        debug_body_bytes = 0
        debug_body_omitted_bytes = 0
        stream_error: str | None = None
        saw_terminal_event = False

        def _append_debug_body_line(line: str) -> None:
            nonlocal debug_body_bytes, debug_body_omitted_bytes
            if not DEBUG_DUMP:
                return

            piece = line if not debug_body_parts else "\n" + line
            encoded = piece.encode("utf-8")
            if DEBUG_DUMP_MAX_BYTES <= 0:
                debug_body_parts.append(piece)
                debug_body_bytes += len(encoded)
                return

            remaining = DEBUG_DUMP_MAX_BYTES - debug_body_bytes
            if remaining <= 0:
                debug_body_omitted_bytes += len(encoded)
                return

            kept_bytes = encoded[:remaining]
            kept = kept_bytes.decode("utf-8", errors="ignore")
            if kept:
                debug_body_parts.append(kept)
                debug_body_bytes += len(kept.encode("utf-8"))
            debug_body_omitted_bytes += len(encoded) - len(kept_bytes)

        def _debug_response_body() -> str:
            body = "".join(debug_body_parts)
            if debug_body_omitted_bytes > 0:
                body += f"... [truncated {debug_body_omitted_bytes} bytes]"
            return body

        def _close_response() -> None:
            close = getattr(response, "close", None)
            if callable(close):
                close()

        # Idle watchdog: a dead upstream stream delivers zero content for its
        # whole (240-277s) life, while a healthy one delivers its first byte
        # within ~83s and then keeps flowing. If no real delta arrives for
        # FLOWITH_STREAM_IDLE_TIMEOUT seconds we abort early and let the caller
        # retry on the empty-stream path instead of waiting out the dead life.
        idle_timeout = idle_timeout if idle_timeout is not None else FLOWITH_STREAM_IDLE_TIMEOUT
        idle_timeout = idle_timeout if idle_timeout > 0 else 0.0
        last_activity = [time.time()]
        idle_timed_out = threading.Event()
        watcher_stop = threading.Event()

        def _mark_activity() -> None:
            last_activity[0] = time.time()

        watcher: threading.Thread | None = None
        if cancel_event is not None or idle_timeout > 0:
            def _watch() -> None:
                while not watcher_stop.is_set():
                    if cancel_event is not None and cancel_event.is_set():
                        _close_response()
                        return
                    if idle_timeout > 0:
                        idle = time.time() - last_activity[0]
                        if idle >= idle_timeout:
                            idle_timed_out.set()
                            _close_response()
                            return
                        wait_for = min(1.0, max(0.05, idle_timeout - idle))
                    else:
                        wait_for = 1.0
                    watcher_stop.wait(wait_for)

            watcher = threading.Thread(target=_watch, daemon=True)
            watcher.start()

        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                if cancel_event is not None and cancel_event.is_set():
                    stream_error = "Upstream stream cancelled"
                    break
                if not raw_line:
                    continue
                line = raw_line.strip()
                _append_debug_body_line(line)

                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    saw_terminal_event = True
                    break
                try:
                    chunk = json.loads(line)
                except Exception:
                    continue

                if "error" in chunk and not chunk.get("choices"):
                    err_info = chunk["error"]
                    err_msg = err_info.get("message", str(err_info)) if isinstance(err_info, dict) else str(err_info)
                    stream_error = err_msg
                    if finish_reason is None:
                        finish_reason = f"error: {err_msg}"
                    break

                if "model" in chunk and not upstream_model:
                    upstream_model = chunk["model"]
                if "usage" in chunk:
                    usage = chunk.get("usage", {}) or usage

                for choice in chunk.get("choices", []) or []:
                    delta = choice.get("delta", {}) or {}
                    fr = choice.get("finish_reason")
                    if fr:
                        finish_reason = fr
                        saw_terminal_event = True

                    piece_raw = delta.get("content")
                    piece_text, piece_thinking = _extract_content_and_thinking(piece_raw)
                    if piece_text:
                        _mark_activity()
                        content_parts.append(piece_text)
                        if on_chunk:
                            on_chunk(piece_text)
                    if piece_thinking:
                        _mark_activity()
                        reasoning_parts.append(piece_thinking)
                        if on_reasoning:
                            on_reasoning(piece_thinking)

                    reasoning = _pick_reasoning_field(delta)
                    if reasoning:
                        _mark_activity()
                        reasoning_parts.append(reasoning)
                        if on_reasoning:
                            on_reasoning(reasoning)

                    tc_deltas = delta.get("tool_calls")
                    if tc_deltas:
                        _mark_activity()
                        for tc in tc_deltas:
                            idx = tc.get("index", 0)
                            if idx not in tool_call_accum:
                                tool_call_accum[idx] = {
                                    "id": tc.get("id", ""),
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            entry = tool_call_accum[idx]
                            if tc.get("id"):
                                entry["id"] = tc["id"]
                            func_delta = tc.get("function", {})
                            if func_delta.get("name"):
                                entry["function"]["name"] = func_delta["name"]
                            if func_delta.get("arguments"):
                                entry["function"]["arguments"] += func_delta["arguments"]
        except Exception as exc:
            # The watchdog aborts a dead/cancelled stream by closing the socket
            # out from under iter_lines(), which surfaces here as a read error.
            # When that close was intentional, swallow it and let the finally
            # block set the idle/cancel stream_error; otherwise it's a genuine
            # mid-stream failure and we record it for the retry path.
            if not (idle_timed_out.is_set() or (cancel_event is not None and cancel_event.is_set())):
                if stream_error is None:
                    stream_error = f"Upstream stream read error: {exc}"
        finally:
            watcher_stop.set()
            _close_response()
            if idle_timed_out.is_set() and stream_error is None:
                stream_error = (
                    f"Upstream stream idle for {idle_timeout:.0f}s "
                    "(no content delta); aborted as a dead stream"
                )
            if cancel_event is not None and cancel_event.is_set():
                if stream_error is None:
                    stream_error = "Upstream stream cancelled"

        _dump_intercept_async(
            payload=payload,
            response_status=response.status_code,
            response_headers=dict(response.headers),
            response_body=_debug_response_body(),
            is_stream=True,
            upstream_model=upstream_model,
        )

        tool_calls: list[dict[str, Any]] | None = None
        if tool_call_accum:
            tool_calls = [tool_call_accum[i] for i in sorted(tool_call_accum)]
            if on_tool_call:
                for tc in tool_calls:
                    on_tool_call(tc)

        if stream_error is None and not saw_terminal_event:
            stream_error = "Upstream stream ended before completion"

        if stream_error:
            return {
                "success": False,
                "error": stream_error,
                "content": "".join(content_parts),
                "time_ms": elapsed_ms,
                "usage": usage,
                "reasoning_content": "".join(reasoning_parts),
                "tool_calls": tool_calls,
                "finish_reason": finish_reason,
                "upstream_model": upstream_model,
                "idle_timeout": idle_timed_out.is_set(),
            }

        return {
            "success": True,
            "content": "".join(content_parts),
            "time_ms": elapsed_ms,
            "usage": usage,
            "reasoning_content": "".join(reasoning_parts),
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
            "upstream_model": upstream_model,
        }
