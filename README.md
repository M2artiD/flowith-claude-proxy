# Flowith Claude/Codex Proxy

This project wraps the Flowith upstream LLM endpoint as local Claude/Anthropic-compatible and OpenAI-compatible proxy services.

There are two launch modes:

- **Claude Code / Anthropic-compatible**: `start.bat`, default base URL `http://127.0.0.1:8787`
- **Codex / OpenAI-compatible**: `start-codex.bat`, default base URL `http://127.0.0.1:8788/v1`

Use the script for the client you are configuring. If you need both clients at the same time, run both scripts in separate terminal windows.

## Start

Claude Code / Anthropic-compatible:

```powershell
.\start.bat
```

Codex / OpenAI-compatible:

```powershell
.\start-codex.bat
```

Both scripts enter `claude-proxy\`, create or reuse `venv`, install dependencies, and then run `python -m proxy`.

- `start.bat` temporarily sets `FLOWITH_API_PROFILE=claude` and enables only Claude/Anthropic routes.
- `start-codex.bat` temporarily sets `FLOWITH_API_PROFILE=codex`, overrides the port to `8788`, and enables only Codex/OpenAI routes.

These environment changes apply only to the launched process and do not rewrite `.env`.

## Claude Code / Anthropic configuration

Use this mode for Claude Code or any Anthropic-compatible client.

```text
Name: Flowith Claude Proxy
API Key: your Flowith API key
Base URL: http://127.0.0.1:8787
Model: claude-4.6-sonnet
```

PowerShell example:

```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
$env:ANTHROPIC_API_KEY="your Flowith API key"
claude
```

Main route:

```text
POST /v1/messages
```

Important: `/v1/models` is intentionally disabled in the Claude profile. If `http://127.0.0.1:8787/v1/models` returns `404 {"detail":"Endpoint not enabled for this proxy profile"}`, the Claude proxy is behaving as designed.

Tool calls use an XML/ReAct bridge: client tools are injected into the prompt as XML instructions, upstream `<tool_call>` output is converted back into client-native tool calls, and tool results are returned as `<observation>` blocks.

## Codex / OpenAI-compatible configuration

Use this mode for Codex or OpenAI-compatible clients.

```text
Name: Flowith Codex Proxy
API Key: your Flowith API key
Base URL: http://127.0.0.1:8788/v1
Model: claude-4.6-sonnet
```

PowerShell example:

```powershell
$env:OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
$env:OPENAI_API_KEY="your Flowith API key"
```

Main routes:

```text
POST /v1/responses
POST /v1/chat/completions
GET  /v1/models
```

Do not point Codex/OpenAI clients at `8787`; that port is for the Claude profile and will reject OpenAI-only routes.

## `.env`

Keep `.env` in `claude-proxy\`. Recommended baseline:

```env
FLOWITH_API_KEY=flo-your-key
FLOWITH_BASE_URL=https://edge.flowith.io/external/use/llm
FLOWITH_DEFAULT_MODEL=claude-4.6-sonnet
FLOWITH_TIMEOUT=300
FLOWITH_CONNECT_TIMEOUT=30
FLOWITH_MAX_CONCURRENCY=8
FLOWITH_POOL_MAXSIZE=16
FLOWITH_RETRY_TOTAL=6
FLOWITH_RETRY_BACKOFF=0.5
FLOWITH_RETRY_JITTER=0.25
FLOWITH_RETRY_MAX_DELAY=8
FLOWITH_SSL_RETRY_EXTRA=10
FLOWITH_DISABLE_KEEPALIVE=true
FLOWITH_SSL_VERIFY=true
FLOWITH_API_HOST=127.0.0.1
FLOWITH_API_PORT=8787
FLOWITH_TOOL_MODE=xml
FLOWITH_MODEL_ALIASES={"claude-4.6-sonnet":"claude-4.6-sonnet","claude-opus-4.7":"claude-opus-4.7","claude-opus-4.8":"claude-opus-4.8","claude-haiku-4-5":"claude-haiku-4-5","claude-fable-5":"claude-fable-5","gpt-5.5":"gpt-5.5","gpt-5.4":"gpt-5.4","gpt-4.1":"gpt-4.1","gemini-2.5-pro":"gemini-2.5-pro","deepseek-chat":"deepseek-chat"}
FLOWITH_DEBUG_DUMP=false
```

`start-codex.bat` overrides `FLOWITH_API_PROFILE=codex` and `FLOWITH_API_PORT=8788` only for that process.

## Available model aliases

- `claude-fable-5`
- `claude-4.6-sonnet`
- `claude-opus-4.7`
- `claude-opus-4.8`
- `claude-haiku-4-5`
- `gpt-5.5`
- `gpt-5.4`
- `gpt-4.1`
- `gemini-2.5-pro`
- `deepseek-chat`

## Debugging and tests

Enable upstream request/response dumps:

```env
FLOWITH_DEBUG_DUMP=true
```

Run tests:

```powershell
cd C:\Users\qiyan\Desktop\flowith-claude-proxy\claude-proxy
.\venv\Scripts\python.exe -m pytest -q
```
