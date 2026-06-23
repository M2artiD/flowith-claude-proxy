# Flowith Proxy Service

This directory contains the actual FastAPI proxy service. Prefer launching from the repository root:

- Claude Code / Anthropic-compatible: `..\start.bat`
- Codex / OpenAI-compatible: `..\start-codex.bat`

You can also run the local scripts in this directory:

- Claude Code / Anthropic-compatible: `start.bat`
- Codex / OpenAI-compatible: `start-codex.bat`

## Claude Code / Anthropic-compatible

Start:

```powershell
.\start.bat
```

Configure the client with:

```text
Base URL: http://127.0.0.1:8787
API Key: your Flowith API key
Model: claude-4.6-sonnet
```

Main route:

```text
POST /v1/messages
```

The Claude profile intentionally does not expose OpenAI routes such as `/v1/models`.

## Codex / OpenAI-compatible

Start:

```powershell
.\start-codex.bat
```

Configure the client with:

```text
Base URL: http://127.0.0.1:8788/v1
API Key: your Flowith API key
Model: claude-4.6-sonnet
```

Main routes:

```text
POST /v1/responses
POST /v1/chat/completions
GET  /v1/models
```

## Tool bridge

Both profiles use the XML/ReAct tool bridge. Client tools are described to the upstream model using XML instructions; upstream `<tool_call>` output is converted back to the client tool-call format; tool results are returned as `<observation>` blocks.

## Configuration

`.env` is loaded from this directory. The launch scripts set profile-specific variables only for the current process:

- `start.bat`: `FLOWITH_API_PROFILE=claude`
- `start-codex.bat`: `FLOWITH_API_PROFILE=codex` and `FLOWITH_API_PORT=8788`

Useful upstream stability settings:

```env
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
```

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest -q
```
