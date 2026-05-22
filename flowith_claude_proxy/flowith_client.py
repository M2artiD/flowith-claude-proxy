"""Minimal HTTP client for the Flowith LLM endpoint.

Mirrors only what the proxy needs: a ``call_api`` that supports both
blocking and streaming modes, returning a normalized dict.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import requests
from requests.exceptions import RequestException, SSLError


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

        self._session = requests.Session()
        self._session.verify = ssl_verify
        self._session.proxies = proxies or {}
        self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})

        if not ssl_verify:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

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
    ) -> dict[str, Any]:
        use_model = model or self.model
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
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

                response = self._session.post(
                    self.base_url,
                    json=payload,
                    timeout=self.timeout,
                    stream=stream,
                )
                elapsed_ms = (time.time() - start) * 1000

                if response.status_code != 200:
                    last_error = Exception(
                        f"HTTP {response.status_code}: {response.text[:300]}"
                    )
                else:
                    if stream:
                        return self._parse_stream(
                            response, elapsed_ms, on_chunk, on_reasoning, on_tool_call
                        )

                    result = response.json()
                    if result.get("choices"):
                        msg = result["choices"][0]["message"]
                        tool_calls = msg.get("tool_calls")
                        return {
                            "success": True,
                            "content": msg.get("content", "") or "",
                            "time_ms": elapsed_ms,
                            "usage": result.get("usage", {}) or {},
                            "reasoning_content": msg.get("reasoning_content", "") or "",
                            "tool_calls": tool_calls,
                            "finish_reason": result["choices"][0].get("finish_reason"),
                        }
                    last_error = Exception("Upstream response has no choices")

            except requests.exceptions.Timeout:
                last_error = Exception("Upstream request timed out")
            except SSLError as e:
                last_error = Exception(
                    "Upstream SSL error. If your network uses a proxy/VPN/firewall that intercepts TLS, "
                    "try setting FLOWITH_SSL_VERIFY=false. Original error: "
                    f"{e}"
                )
            except RequestException as e:
                last_error = Exception(f"Upstream request failed: {e}")
            except Exception as e:
                last_error = e

            if attempt < max_retries:
                time.sleep(attempt * 2)

        return {"success": False, "error": str(last_error) if last_error else "unknown error"}

    def _parse_stream(
        self,
        response: requests.Response,
        elapsed_ms: float,
        on_chunk: Callable[[str], None] | None,
        on_reasoning: Callable[[str], None] | None,
        on_tool_call: Callable[[dict[str, Any]], None] | None,
    ) -> dict[str, Any]:
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage: dict[str, Any] = {}
        # Accumulate tool_call fragments: index -> {id, function: {name, arguments}}
        tool_call_accum: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None

        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line:
                continue
            line = raw_line.strip()
            if line.startswith("data:"):
                line = line[5:].strip()
            if line == "[DONE]":
                break
            try:
                chunk = json.loads(line)
            except Exception:
                continue
            if "usage" in chunk:
                usage = chunk.get("usage", {}) or usage
            for choice in chunk.get("choices", []) or []:
                delta = choice.get("delta", {}) or {}
                fr = choice.get("finish_reason")
                if fr:
                    finish_reason = fr

                piece = delta.get("content")
                if piece:
                    content_parts.append(piece)
                    if on_chunk:
                        on_chunk(piece)
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    reasoning_parts.append(reasoning)
                    if on_reasoning:
                        on_reasoning(reasoning)

                # Accumulate tool call deltas
                tc_deltas = delta.get("tool_calls")
                if tc_deltas:
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

        # Finalize tool calls and notify
        tool_calls: list[dict[str, Any]] | None = None
        if tool_call_accum:
            tool_calls = [tool_call_accum[i] for i in sorted(tool_call_accum)]
            if on_tool_call:
                for tc in tool_calls:
                    on_tool_call(tc)

        return {
            "success": True,
            "content": "".join(content_parts),
            "time_ms": elapsed_ms,
            "usage": usage,
            "reasoning_content": "".join(reasoning_parts),
            "tool_calls": tool_calls,
            "finish_reason": finish_reason,
        }
