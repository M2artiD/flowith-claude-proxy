---
name: hermes-proxy-compat
description: Validate and debug Hermes OpenAI-compatible proxy behavior in this project. Use for Hermes `/health`, `/v1/models`, `/v1/chat/completions`, streaming/non-streaming chat, OpenAI tool-call to XML/ReAct bridging, duplicate final text, raw XML leaks, think-tag filtering, or Hermes smoke-test updates around port 8789.
---

# Hermes Proxy Compatibility

Use this project-local skill when Hermes compatibility is the target or when proxy changes can affect Hermes behavior.

## Scope

Hermes is expected to expose an OpenAI-compatible surface:

- `GET /health`
- `GET /v1/models`
- `POST /v1/chat/completions`
- streaming chat completions using SSE
- OpenAI `tools` / `tool_choice` requests bridged to the proxy's XML/ReAct path without leaking raw XML to clients

Default local port is `8789` unless configuration or the user says otherwise.

## Workflow

1. Identify how Hermes is launched in the current repo (`start-hermes.bat`, `start.sh`, Docker, or Python entrypoint).
2. Inspect current config and tests before changing code.
3. For behavior bugs, create or run a focused reproduction against fake upstream chunks when possible.
4. Verify both non-streaming and streaming paths; Hermes regressions often appear only in one mode.
5. For tool calls, assert structured tool-call output and absence of raw XML tags in visible assistant text.
6. For think tags, assert hidden reasoning is filtered while ordinary partial marker text is not lost at final flush.

## Smoke Checks

Prefer the repo script `claude-proxy/scripts/smoke_hermes.ps1` when available. It should cover:

- health endpoint
- model list endpoint
- non-streaming chat
- streaming chat with non-empty deltas
- tool-call request
- no raw XML leak
- no duplicated final answer text
- partial think-tag tail flush behavior

If the Hermes service is not running, report the exact start command or skipped checks instead of implying compatibility is verified.

## Reporting

Final evidence should include:

- base URL and port tested;
- launch method or reason the service was not launched;
- commands run and exit codes;
- endpoints checked;
- whether streaming deltas, final content, tool calls, and XML/think filtering passed.
