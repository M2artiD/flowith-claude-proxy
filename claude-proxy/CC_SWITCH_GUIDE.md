# CC Switch Import Guide

Use this guide to add the local Flowith proxy to CC Switch.

## Choose the right provider type

Create separate providers for Claude-compatible and OpenAI-compatible clients:

| Client | Launch script | Base URL | Notes |
|---|---|---|---|
| Claude Code / Anthropic-compatible | `start.bat` | `http://127.0.0.1:8787` | Uses `POST /v1/messages` |
| Codex / OpenAI-compatible | `start-codex.bat` | `http://127.0.0.1:8788/v1` | Uses `/v1/responses`, `/v1/chat/completions`, `/v1/models` |

Do not use the old `http://127.0.0.1:8000` address.

## Claude Code provider

1. Start the proxy:

   ```powershell
   C:\Users\qiyan\Desktop\flowith-claude-proxy\start.bat
   ```

2. Verify health:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8787/health
   ```

3. In CC Switch, add a custom Anthropic-compatible provider:

   | Field | Value |
   |---|---|
   | Name | `Flowith Claude Proxy` |
   | API Key | your Flowith API key |
   | Base URL | `http://127.0.0.1:8787` |
   | Model | `claude-5-sonnet` |

4. Enable the provider and restart Claude Code if needed.

Deep link example:

```text
ccswitch://provider/import?name=Flowith%20Claude%20Proxy&apiKey=your-flowith-api-key&baseUrl=http://127.0.0.1:8787&model=claude-5-sonnet
```

Note: `GET /v1/models` is disabled on `8787` by design. Use `8788/v1` for OpenAI-compatible model listing.

## Codex / OpenAI-compatible provider

1. Start the proxy:

   ```powershell
   C:\Users\qiyan\Desktop\flowith-claude-proxy\start-codex.bat
   ```

2. Verify models:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:8788/v1/models -Headers @{Authorization='Bearer your-flowith-api-key'}
   ```

3. In CC Switch, add a custom OpenAI-compatible provider:

   | Field | Value |
   |---|---|
   | Name | `Flowith Codex Proxy` |
   | API Key | your Flowith API key |
   | Base URL | `http://127.0.0.1:8788/v1` |
   | Model | `claude-5-sonnet` |

Deep link example:

```text
ccswitch://provider/import?name=Flowith%20Codex%20Proxy&apiKey=your-flowith-api-key&baseUrl=http://127.0.0.1:8788/v1&model=claude-5-sonnet
```

## Troubleshooting

### Connection refused

Check that the matching script is running and that the client is using the matching port:

- Claude/Anthropic: `8787`
- Codex/OpenAI: `8788/v1`

### `/v1/models` returns 404 on `8787`

This is expected. `8787` runs the Claude profile and only exposes Claude/Anthropic routes. Start `start-codex.bat` and use `http://127.0.0.1:8788/v1/models` for OpenAI-compatible model listing.

### Wrong model or route

Use a currently configured alias such as:

- `claude-5-sonnet`
- `claude-opus-4.7`
- `claude-opus-4.8`
- `claude-haiku-4-5`
- `claude-fable-5`
- `gpt-5.5`
- `gpt-5.4`
- `gpt-4.1`
- `gemini-2.5-pro`
- `deepseek-chat`
