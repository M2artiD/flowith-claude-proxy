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

    def call_api(
        self,
        messages: list[dict[str, Any]],
        model: str | None = None,
        max_retries: int = 1,
        stream: bool = False,
        on_chunk: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        use_model = model or self.model
        last_error: Exception | None = None

        for attempt in range(1, max_retries + 1):
            try:
                start = time.time()
                response = requests.post(
                    self.base_url,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "models": [use_model],
                        "messages": messages,
                        "stream": stream,
                        "thinking": self.thinking,
                    },
                    timeout=self.timeout,
                    stream=stream,
                    verify=self.ssl_verify,
                    proxies=self.proxies,
                )
                elapsed_ms = (time.time() - start) * 1000

                if response.status_code != 200:
                    last_error = Exception(
                        f"HTTP {response.status_code}: {response.text[:300]}"
                    )
                else:
                    if stream:
                        content_parts: list[str] = []
                        reasoning_parts: list[str] = []
                        usage: dict[str, Any] = {}
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
                                piece = delta.get("content")
                                if piece:
                                    content_parts.append(piece)
                                    if on_chunk:
                                        on_chunk(piece)
                                reasoning = delta.get("reasoning_content")
                                if reasoning:
                                    reasoning_parts.append(reasoning)
                        return {
                            "success": True,
                            "content": "".join(content_parts),
                            "time_ms": elapsed_ms,
                            "usage": usage,
                            "reasoning_content": "".join(reasoning_parts),
                        }

                    result = response.json()
                    if result.get("choices"):
                        msg = result["choices"][0]["message"]
                        return {
                            "success": True,
                            "content": msg.get("content", ""),
                            "time_ms": elapsed_ms,
                            "usage": result.get("usage", {}) or {},
                            "reasoning_content": msg.get("reasoning_content", "") or "",
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
