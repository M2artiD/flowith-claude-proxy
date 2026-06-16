# Flowith Claude/Codex Proxy

本项目把 Flowith 上游接口包装成本地代理。现在按两个脚本使用：

- Claude Code / Anthropic: `start.bat`, 默认 `http://127.0.0.1:8787`
- Codex / OpenAI-compatible: `start-codex.bat`, 默认 `http://127.0.0.1:8788/v1`

这样不用把 Claude Code 和 Codex 混在同一个启动入口里。需要哪个就开哪个；如果两个都要用，就开两个窗口。

## 启动

Claude Code:

```powershell
.\start.bat
```

Codex / OpenAI-compatible:

```powershell
.\start-codex.bat
```

两个脚本都会进入 `claude-proxy/`，创建/启用 venv，安装依赖，然后启动 `python -m proxy`。

- `start.bat` 设置 `FLOWITH_API_PROFILE=claude`，只启用 Claude/Anthropic 入口
- `start-codex.bat` 设置 `FLOWITH_API_PROFILE=codex`，只启用 Codex/OpenAI 入口，并临时把端口覆盖成 `8788`

这些设置只作用于当前窗口，不会修改你的 `.env`。

## Claude Code 配置

Claude Code 或 CC Switch 的 Anthropic-compatible provider 填：

```text
Name: Flowith Claude Proxy
API Key: 你的 Flowith API Key
Base URL: http://127.0.0.1:8787
Model: claude-4.6-sonnet
```

环境变量方式：

```powershell
$env:ANTHROPIC_BASE_URL="http://127.0.0.1:8787"
$env:ANTHROPIC_API_KEY="你的 Flowith API Key"
claude
```

Claude Code 工具调用参考 `vibheksoni/UniClaudeProxy` 的 ReAct/XML fallback：

1. 接收 Anthropic `tools`
2. 注入 XML 工具说明到 system prompt
3. 上游输出 `<tool_call>`
4. 代理转换回 Anthropic `tool_use`
5. `tool_result` 转成 `<observation>` 继续对话

## Codex / OpenAI 配置

Codex 或 CC Switch 的 OpenAI-compatible provider 填：

```text
Name: Flowith Codex Proxy
API Key: 你的 Flowith API Key
Base URL: http://127.0.0.1:8788/v1
Model: claude-4.6-sonnet
```

也可以直接填：

```text
Model: gpt-5.5
Model: gpt-5.4
```

环境变量方式：

```powershell
$env:OPENAI_BASE_URL="http://127.0.0.1:8788/v1"
$env:OPENAI_API_KEY="你的 Flowith API Key"
```

Codex 脚本使用的主要接口：

```text
POST /v1/responses
POST /v1/chat/completions
GET  /v1/models
```

Codex 工具调用也走 `vibheksoni/UniClaudeProxy` 同类的 ReAct/XML fallback：
1. 接收 OpenAI Responses `tools`
2. 转成 XML 工具说明注入 system prompt
3. 上游只输出 `<tool_call>`
4. 代理转换回 Responses `function_call`
5. `function_call_output` 转成 `<observation>` 继续对话

也就是说，Codex/CC Switch 侧仍然按 OpenAI-compatible 填 `http://127.0.0.1:8788/v1`，不需要 Flowith 上游原生支持工具。

## `.env` 是否还能用

可以。旧 `.env` 仍然兼容。建议保留或补上：

```env
FLOWITH_API_KEY=flo-your-key
FLOWITH_BASE_URL=https://edge.flowith.io/external/use/llm
FLOWITH_DEFAULT_MODEL=claude-4.6-sonnet
FLOWITH_TIMEOUT=120
FLOWITH_SSL_VERIFY=true
FLOWITH_API_HOST=127.0.0.1
FLOWITH_API_PORT=8787
FLOWITH_TOOL_MODE=xml
FLOWITH_MODEL_ALIASES={"claude-4.6-sonnet":"claude-4.6-sonnet","claude-opus-4.7":"claude-opus-4.7","claude-opus-4.8":"claude-opus-4.8","claude-haiku-4-5":"claude-haiku-4-5","claude-fable-5":"claude-fable-5","gpt-5.5":"gpt-5.5","gpt-5.4":"gpt-5.4","gpt-4.1":"gpt-4.1","gemini-2.5-pro":"gemini-2.5-pro","deepseek-chat":"deepseek-chat"}
FLOWITH_DEBUG_DUMP=false
```

`start-codex.bat` 会在进程内临时使用 `FLOWITH_API_PROFILE=codex` 和 `FLOWITH_API_PORT=8788`，不会写回 `.env`。

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

## 调试与测试

开启 dump:

```env
FLOWITH_DEBUG_DUMP=true
```

运行测试：

```powershell
cd C:\Users\qiyan\Desktop\flowith-claude-proxy\claude-proxy
python -m pytest -q
```
