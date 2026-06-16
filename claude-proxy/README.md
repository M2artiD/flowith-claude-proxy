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
