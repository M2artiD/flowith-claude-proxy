# Flowith Proxy Service

This directory contains the actual FastAPI proxy service. Prefer launching from the repository root:

- Claude Code / Anthropic-compatible: `..\start.bat`
- Codex / OpenAI-compatible: `..\start-codex.bat`
- Hermes Agent / OpenAI-compatible tool bridge: `..\start-hermes.bat`

You can also run the local scripts in this directory:

- Claude Code / Anthropic-compatible: `start.bat`
- Codex / OpenAI-compatible: `start-codex.bat`
- Hermes Agent / OpenAI-compatible tool bridge: `start-hermes.bat`

If the target port already has a healthy local proxy, the launcher opens the dashboard, prints `[OK] Proxy already running...`, and exits successfully. Launchers open `http://127.0.0.1:<port>/dashboard` after `/health` succeeds; set `FLOWITH_OPEN_DASHBOARD=false` (or `0`) in the environment or `.env` to disable this.

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
GET  /models
```

## Hermes Agent / OpenAI-compatible

Start:

```powershell
.\start-hermes.bat
```

Configure Hermes with:

```text
Base URL: http://127.0.0.1:8789/v1
API Key: your Flowith API key
Model: claude-4.6-sonnet
```

Main routes:

```text
POST /v1/chat/completions
POST /chat/completions
GET  /v1/models
GET  /models
```

`start-hermes.bat` uses port `8789` so it can run beside the Codex launcher on `8788`.

## Local dashboard

The service exposes a read-only dashboard at `/dashboard`, with API endpoints under `/dashboard/api/*` for health, route inventory, safe masked config, and debug dump metadata. The dashboard never returns raw debug dump contents.

## Tool bridge

The profiles use the XML/ReAct tool bridge. Client tools are described to the upstream model using XML instructions; upstream `<tool_call>` output is converted back to the client tool-call format; tool results are returned as `<observation>` blocks.

## Configuration

`.env` is loaded from this directory. The launch scripts set profile-specific variables only for the current process:

- `start.bat`: `FLOWITH_API_PROFILE=claude` and `FLOWITH_API_PORT=8787`
- `start-codex.bat`: `FLOWITH_API_PROFILE=codex` and `FLOWITH_API_PORT=8788`
- `start-hermes.bat`: `FLOWITH_API_PROFILE=codex`, `FLOWITH_API_PORT=8789`, and `FLOWITH_TOOL_MODE=xml`

Useful upstream stability settings:

```env
FLOWITH_TIMEOUT=300
FLOWITH_CONNECT_TIMEOUT=30
FLOWITH_MAX_CONCURRENCY=8
FLOWITH_SEMAPHORE_TIMEOUT=30
FLOWITH_POOL_MAXSIZE=16
FLOWITH_RETRY_TOTAL=6
FLOWITH_RETRY_BACKOFF=0.5
FLOWITH_RETRY_JITTER=0.25
FLOWITH_RETRY_MAX_DELAY=8
FLOWITH_SSL_RETRY_EXTRA=10
FLOWITH_DISABLE_KEEPALIVE=true
FLOWITH_SSL_VERIFY=true
FLOWITH_MAX_REQUEST_BYTES=20971520
FLOWITH_OPEN_DASHBOARD=true
FLOWITH_DEBUG_DUMP=false
FLOWITH_DEBUG_DUMP_DIR=
FLOWITH_DEBUG_DUMP_MAX_BYTES=1048576
FLOWITH_DEBUG_DUMP_MAX_FILES=100
```

If `.env` is missing, the launch scripts copy `.env.example` to `.env` and ask you to fill in `FLOWITH_API_KEY`.

Security defaults:

- `FLOWITH_API_HOST=127.0.0.1`
- `FLOWITH_LOCAL_ONLY=true`
- `FLOWITH_REQUIRE_SERVER_KEY=false`

If you bind the proxy to a non-local interface, set `FLOWITH_REQUIRE_SERVER_KEY=true` and restrict the port with a firewall. The proxy refuses to start with `FLOWITH_LOCAL_ONLY=false` and `FLOWITH_REQUIRE_SERVER_KEY=false` because that would expose an unauthenticated upstream relay.

Resource and debug safety:

- `FLOWITH_MAX_REQUEST_BYTES` rejects oversized request bodies with HTTP 413 before JSON parsing. Set `0` only for a trusted local-only deployment.
- `FLOWITH_SEMAPHORE_TIMEOUT` bounds how long a request waits for an upstream concurrency slot before returning a failure instead of tying up a worker indefinitely.
- `FLOWITH_DEBUG_DUMP` is for local troubleshooting only. Dumps are written under `debug_dumps/`, may contain sensitive prompts or code, are redacted/truncated, and are pruned by `FLOWITH_DEBUG_DUMP_MAX_BYTES` / `FLOWITH_DEBUG_DUMP_MAX_FILES`; do not enable it on shared or production hosts.

Docker:

```powershell
copy .env.example .env
notepad .env
docker compose up --build
```

The Compose file binds to `127.0.0.1:8787` and sets `FLOWITH_REQUIRE_SERVER_KEY=true`. The Docker image runs the proxy as a non-root `proxy` user.

## Tests

```powershell
.\venv\Scripts\python.exe -m pytest -q
```
