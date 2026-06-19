# Flowith Proxy Service

这里是实际服务代码目录。推荐从仓库根目录启动：

- Claude Code: `..\start.bat`
- Codex/OpenAI: `..\start-codex.bat`

也可以在当前目录启动：

- Claude Code: `start.bat`
- Codex/OpenAI: `start-codex.bat`

## Claude Code

启动：

```powershell
.\start.bat
```

配置：

```text
Base URL: http://127.0.0.1:8787
API Key: 你的 Flowith API Key
Model: claude-4.6-sonnet
```

主要接口：

```text
POST /v1/messages
```

工具调用参考 `vibheksoni/UniClaudeProxy` 的 ReAct/XML fallback。

## Codex / OpenAI-compatible

启动：

```powershell
.\start-codex.bat
```

配置：

```text
Base URL: http://127.0.0.1:8788/v1
API Key: 你的 Flowith API Key
Model: claude-4.6-sonnet
```

也可以用：

```text
Model: gpt-5.5
Model: gpt-5.4
```

主要接口：

```text
POST /v1/responses
POST /v1/chat/completions
GET  /v1/models
```

Codex 工具调用同样参考 `vibheksoni/UniClaudeProxy` 的 ReAct/XML fallback：
1. 接收 OpenAI Responses `tools`
2. 注入 XML 工具说明
3. 把上游 `<tool_call>` 转回 Responses `function_call`
4. 把 `function_call_output` 转成 `<observation>` 继续对话

## 配置

`.env` 继续使用 Flowith 配置。

- `start.bat` 临时设置 `FLOWITH_API_PROFILE=claude`
- `start-codex.bat` 临时设置 `FLOWITH_API_PROFILE=codex` 和 `FLOWITH_API_PORT=8788`

这些设置只在当前进程生效，不会改写 `.env`。

推荐模型别名保持同名：

```env
FLOWITH_MODEL_ALIASES={"claude-4.6-sonnet":"claude-4.6-sonnet","claude-opus-4.7":"claude-opus-4.7","claude-opus-4.8":"claude-opus-4.8","claude-haiku-4-5":"claude-haiku-4-5","claude-fable-5":"claude-fable-5","gpt-5.5":"gpt-5.5","gpt-5.4":"gpt-5.4","gpt-4.1":"gpt-4.1","gemini-2.5-pro":"gemini-2.5-pro","deepseek-chat":"deepseek-chat"}
```

## 可用模型

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

## 测试

```powershell
python -m pytest -q
```
## 上游稳定性配置

代理默认会对 Flowith 上游请求做连接复用、限流和自动重试。常用参数：

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

说明：

- `FLOWITH_TIMEOUT` 是读取响应的最长等待时间，适合长回答或流式输出。
- `FLOWITH_CONNECT_TIMEOUT` 是建立连接的等待时间，连接失败会更快进入重试。
- `FLOWITH_RETRY_TOTAL` 是全局最小请求尝试次数；即使某些流式入口传入较低重试次数，也会使用该下限。
- `FLOWITH_RETRY_MAX_DELAY` 会限制指数退避的最大等待时间，避免重连间隔过长。
- `FLOWITH_SSL_VERIFY` 默认保持 `true`；SSL EOF 或临时网络错误会重建 session 后重试，不建议为了规避偶发错误关闭证书校验。
