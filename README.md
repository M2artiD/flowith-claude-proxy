# Flowith Claude/Codex Proxy

This project wraps the Flowith upstream LLM endpoint as local Claude/Anthropic-compatible and OpenAI-compatible proxy services.

There are three launch modes:

- **Claude Code / Anthropic-compatible**: `start.bat`, default base URL `http://127.0.0.1:8787`
- **Codex / OpenAI-compatible**: `start-codex.bat`, default base URL `http://127.0.0.1:8788/v1`
- **Hermes Agent / OpenAI-compatible tool bridge**: `start-hermes.bat`, default base URL `http://127.0.0.1:8789/v1`

Use the script for the client you are configuring. If you need multiple clients at the same time, run their scripts in separate terminal windows.

## Start

Claude Code / Anthropic-compatible:

```powershell
.\start.bat
```

Codex / OpenAI-compatible:

```powershell
.\start-codex.bat
```

Hermes Agent / OpenAI-compatible tool bridge:

```powershell
.\start-hermes.bat
```

All launch scripts enter `claude-proxy\`, create or reuse `venv`, install dependencies, and then run `python -m proxy`. On first run, the launcher copies `claude-proxy\.env.example` to `claude-proxy\.env`; edit that file and set `FLOWITH_API_KEY`.

- `start.bat` temporarily sets `FLOWITH_API_PROFILE=claude` and enables only Claude/Anthropic routes.
- `start-codex.bat` temporarily sets `FLOWITH_API_PROFILE=codex`, overrides the port to `8788`, and enables Codex/OpenAI routes.
- `start-hermes.bat` temporarily sets `FLOWITH_API_PROFILE=codex`, `FLOWITH_TOOL_MODE=xml`, overrides the port to `8789`, and enables the OpenAI-compatible XML tool bridge surface.
- Each launcher opens `http://127.0.0.1:<port>/dashboard` after the local health check succeeds. Set `FLOWITH_OPEN_DASHBOARD=false` (or `0`) in the environment or `claude-proxy\.env` to disable auto-open.
- If the target port already has a healthy local proxy, the launcher opens the dashboard, prints `[OK] Proxy already running...`, and exits successfully.

These environment changes apply only to the launched process and do not rewrite `.env`.

## Clean

If a launcher was closed mid-startup or local caches look stale, run:

```powershell
.\clean.bat
```

This removes the dependency install lock when it is empty, plus local pytest and Python bytecode caches. It keeps `.env` and `claude-proxy\venv` by default, and skips log files that are still held by a running proxy. To force dependencies to reinstall on the next launch:

```powershell
.\clean.bat --venv
```

To stop local proxy listeners on ports `8787`, `8788`, and `8789` before cleanup:

```powershell
.\clean.bat --stop-proxy
```

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
GET  /models
```

Do not point Codex/OpenAI clients at `8787`; that port is for the Claude profile and will reject OpenAI-only routes.

## Hermes Agent / OpenAI-compatible configuration

Use this mode for Hermes or other OpenAI-compatible clients that need the XML tool bridge on a separate local port.

```text
Name: Flowith Hermes Proxy
API Key: your Flowith API key
Base URL: http://127.0.0.1:8789/v1
Model: claude-4.6-sonnet
```

Main routes:

```text
POST /v1/chat/completions
POST /chat/completions
GET  /v1/models
GET  /models
```

Do not point Hermes at `8788` if you are also running Codex; `start-hermes.bat` uses `8789` so both clients can run at the same time.

## Local dashboard

Every local proxy exposes a read-only dashboard at:

```text
http://127.0.0.1:<port>/dashboard
```

The dashboard shows health, route inventory, safe masked configuration, model aliases, and debug dump metadata. Launchers open it automatically after `/health` reports ok; set `FLOWITH_OPEN_DASHBOARD=false` (or `0`) in the environment or `claude-proxy\.env` to keep the browser closed.

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
FLOWITH_LOCAL_ONLY=true
FLOWITH_REQUIRE_SERVER_KEY=false
FLOWITH_OPEN_DASHBOARD=true
FLOWITH_TOOL_MODE=xml
FLOWITH_MODEL_ALIASES={"claude-4.6-sonnet":"claude-4.6-sonnet","claude-opus-4.7":"claude-opus-4.7","claude-opus-4.8":"claude-opus-4.8","claude-haiku-4-5":"claude-haiku-4-5","claude-fable-5":"claude-fable-5","gpt-5.5":"gpt-5.5","gpt-5.4":"gpt-5.4","gpt-4.1":"gpt-4.1","gemini-2.5-pro":"gemini-2.5-pro","deepseek-chat":"deepseek-chat"}
FLOWITH_DEBUG_DUMP=false
```

`start-codex.bat` overrides `FLOWITH_API_PROFILE=codex` and `FLOWITH_API_PORT=8788` only for that process. `start-hermes.bat` overrides `FLOWITH_API_PROFILE=codex`, `FLOWITH_API_PORT=8789`, and `FLOWITH_TOOL_MODE=xml` only for that process.

## Security defaults

The proxy is local-first by default:

- `FLOWITH_API_HOST=127.0.0.1` binds to localhost.
- `FLOWITH_LOCAL_ONLY=true` rejects non-local clients even if the bind host is changed.
- `FLOWITH_REQUIRE_SERVER_KEY=false` allows clients to supply their own Flowith key. Set it to `true` when exposing the proxy beyond your own machine.

Do not bind to `0.0.0.0` unless you also understand who can reach the port. If you intentionally expose it, prefer `FLOWITH_REQUIRE_SERVER_KEY=true` and firewall the port.

## Docker

From `claude-proxy\`:

```powershell
copy .env.example .env
notepad .env
docker compose up --build
```

The bundled Compose file maps `127.0.0.1:8787:8787`, uses the current `FLOWITH_*` variables, and requires the client API key to match `FLOWITH_API_KEY`.

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
