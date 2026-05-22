# flowith-claude-proxy

A tiny local HTTP server that exposes the **Anthropic Claude `/v1/messages`
API** while forwarding everything to **Flowith**'s LLM endpoint.

Use it to make any Claude-compatible tool — **Claude Code**, **cc-switch**,
the official Anthropic SDKs, Cline, etc. — talk to Flowith-hosted models
(GPT-5.x, Claude 4.6, Gemini 2.5, ...).

[**中文文档**](docs/USAGE.zh-CN.md)

```
┌──────────────────┐   Anthropic format    ┌─────────────────────┐   Flowith format    ┌──────────────────┐
│ Claude Code /    │ ───────────────────►  │ flowith-claude-     │ ──────────────────► │ edge.flowith.io  │
│ cc-switch / SDK  │ ◄───────────────────  │ proxy (this server) │ ◄────────────────── │                  │
└──────────────────┘    SSE / JSON          └─────────────────────┘    SSE / JSON      └──────────────────┘
```

## Features

- **Strict Anthropic protocol output**: response shape, `usage` fields
  (including `cache_creation_input_tokens` / `cache_read_input_tokens`),
  and the full SSE event order
  (`message_start → ping → content_block_start → content_block_delta* → content_block_stop → message_delta → message_stop`).
- **Streaming** (SSE) and **non-streaming** both supported.
- **Model alias mapping**: `claude-3-5-sonnet-20241022`, `claude-sonnet-4-5`,
  `claude-opus-4-1`, ... → Flowith equivalents. Native Flowith ids
  (`gpt-5.4`, `gemini-2.5-pro`, ...) pass through as-is.
- **Custom model aliases via env var**: override or extend built-in mappings
  without editing source code (`FLOWITH_MODEL_ALIASES`).
- **Bring-your-own key**: clients may send `x-api-key` or
  `Authorization: Bearer <key>`; otherwise the server's `FLOWITH_API_KEY`
  is used as a fallback.
- **Proxy support**: route upstream requests through a local proxy (Clash,
  V2Ray, etc.) via `FLOWITH_UPSTREAM_PROXY`.
- Zero coupling: standalone project. Drop it anywhere and `pip install`.

## Quick start

**Windows** — double-click `start.ps1` or run:

```powershell
.\start.ps1
```

**Linux / macOS**:

```bash
./start.sh
```

**Docker**:

```bash
docker compose up -d
```

The script handles venv activation, dependency install, and `.env` setup automatically.

---

## Install

```bash
cd flowith-claude-proxy
pip install -e .
```

Or without installing:

```bash
pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and fill in your key (optional — clients
can also supply their own):

```env
FLOWITH_API_KEY=fw_xxxxxxxxxxxxxxxx

# For users behind firewalls/DPI (e.g. China):
FLOWITH_UPSTREAM_PROXY=http://127.0.0.1:7890
FLOWITH_SSL_VERIFY=false
```

All config options:

| Variable | Default | Description |
|----------|---------|-------------|
| `FLOWITH_API_KEY` | — | Fallback API key if client doesn't send one |
| `FLOWITH_UPSTREAM_PROXY` | — | HTTP/SOCKS proxy for upstream requests |
| `FLOWITH_SSL_VERIFY` | `true` | Set `false` if proxy breaks TLS |
| `FLOWITH_BASE_URL` | `https://edge.flowith.io/external/use/llm` | Upstream endpoint |
| `FLOWITH_DEFAULT_MODEL` | `claude-4.6-sonnet` | Default model when not specified |
| `FLOWITH_API_HOST` | `127.0.0.1` | Server bind address |
| `FLOWITH_API_PORT` | `8787` | Server bind port |
| `FLOWITH_TIMEOUT` | `120` | Upstream request timeout (seconds) |
| `FLOWITH_MODEL_ALIASES` | — | JSON string for custom model mapping |

## Run

```bash
flowith-claude-proxy
# or
python -m flowith_claude_proxy
```

Default bind: `http://127.0.0.1:8787`.

CLI options:

```
flowith-claude-proxy --host 0.0.0.0 --port 8787 [--reload]
```

## Use with cc-switch / Claude Code

In **cc-switch**, add a new endpoint:

| Field    | Value                          |
| -------- | ------------------------------ |
| Base URL | `http://127.0.0.1:8787`        |
| API Key  | your Flowith key (or any string if the server already has `FLOWITH_API_KEY`) |
| Model    | e.g. `claude-3-5-sonnet-20241022` (will be mapped) or `gpt-5.4` (used as-is) |

For Claude Code / Anthropic SDK directly:

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=<your-flowith-key>
claude        # or any other Anthropic-compatible client
```

## Quick test

Non-streaming:

```bash
curl http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: $FLOWITH_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 256,
    "messages": [{"role":"user","content":"Say hi in one short sentence."}]
  }'
```

Streaming:

```bash
curl -N http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -H "x-api-key: $FLOWITH_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 256,
    "stream": true,
    "messages": [{"role":"user","content":"Count to 5."}]
  }'
```

## Model mapping

Built-in aliases:

| Claude name (input)                 | Flowith model (upstream) |
| ----------------------------------- | ------------------------ |
| `claude-3-5-sonnet-*`               | `claude-4.6-sonnet`      |
| `claude-3-7-sonnet-*`               | `claude-4.6-sonnet`      |
| `claude-sonnet-4-*` / `claude-sonnet-4-5` | `claude-4.6-sonnet`|
| `claude-opus-4-*` / `claude-3-opus-*` | `claude-opus-4.7`      |
| `claude-3-haiku-*`                  | `claude-4.6-sonnet`      |
| `gpt-5.4`, `gemini-2.5-pro`, ...    | (pass through unchanged) |

Custom aliases (no code changes needed):

```env
FLOWITH_MODEL_ALIASES={"claude-3-5-sonnet-20241022":"claude-opus-4.7"}
```

## Docker

One-command start with compose:

```bash
docker compose up -d
```

Or manual build and run:

```bash
docker build -t flowith-claude-proxy .
docker run -p 8787:8787 --env-file .env flowith-claude-proxy
```

## Limitations

- **Tool use / function calling** is forwarded as plain text
  (`[tool_use ...]` / `[tool_result ...]`) — Flowith doesn't natively
  support Anthropic's tool-call schema.
- **Images** in messages are stripped (`[image omitted]`).
- `max_tokens`, `temperature`, `top_p`, `stop_sequences` from the
  Anthropic request are currently ignored (Flowith's endpoint doesn't
  expose them in the same way).
- Token counts in `usage` come from Flowith and are mapped to
  `input_tokens` / `output_tokens`; cache-related counters are always 0.

## License

[MIT](LICENSE)
