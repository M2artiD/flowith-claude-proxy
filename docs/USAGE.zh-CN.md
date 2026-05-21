# flowith-claude-proxy 使用说明（中文）

本项目是一个本地 HTTP 代理服务：对外暴露 **Anthropic Claude `/v1/messages` API**，对内把请求转发到 **Flowith** 的 LLM 接口。

借助它，任何兼容 Claude 协议的工具（Claude Code、cc-switch、Anthropic 官方 SDK、Cline 等）都可以使用 Flowith 上托管的模型（GPT-5.x、Claude 4.6、Gemini 2.5 等）。

```
┌──────────────────┐  Anthropic 协议  ┌──────────────────────┐  Flowith 协议  ┌──────────────────┐
│ Claude Code /    │ ───────────────► │  flowith-claude-     │ ─────────────► │ edge.flowith.io  │
│ cc-switch / SDK  │ ◄─────────────── │  proxy（本服务）     │ ◄───────────── │                  │
└──────────────────┘   SSE / JSON     └──────────────────────┘   SSE / JSON   └──────────────────┘
```

---

## 一、环境要求

- Python **3.10+**
- 一个有效的 **Flowith API Key**（形如 `fw_xxxxxxxx...`）
- 操作系统：Windows / macOS / Linux 均可

---

## 二、安装

进入项目根目录后任选一种方式：

### 方式 1：作为本地包安装（推荐）

```bash
cd flowith-claude-proxy
pip install -e .
```

安装后会获得一个命令行入口：`flowith-claude-proxy`。

### 方式 2：仅安装依赖

```bash
pip install -r requirements.txt
```

之后用 `python -m flowith_claude_proxy` 启动。

### 方式 3：使用项目自带的虚拟环境（如果存在 .venv）

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
pip install -e .
```

Windows CMD：

```cmd
.venv\Scripts\activate.bat
pip install -e .
```

---

## 三、配置

复制 `.env.example` 为 `.env`，填入你的 Flowith Key：

```env
FLOWITH_API_KEY=fw_你的真实key

# 以下为可选项（默认值已够用）
# FLOWITH_BASE_URL=https://edge.flowith.io/external/use/llm
# FLOWITH_DEFAULT_MODEL=claude-4.6-sonnet
# FLOWITH_API_HOST=127.0.0.1
# FLOWITH_API_PORT=8787
# FLOWITH_TIMEOUT=120
```

> 说明：服务端的 `FLOWITH_API_KEY` 只是**兜底**。客户端也可以通过 `x-api-key` 或 `Authorization: Bearer <key>` 头自带 Key；两者都没有时返回 401。

Key 的读取顺序：

1. 环境变量 `FLOWITH_API_KEY`
2. 项目根目录下的 `.env`
3. 当前工作目录下的 `.env`

---

## 四、启动服务

```bash
flowith-claude-proxy
# 或者
python -m flowith_claude_proxy
```

默认监听 `http://127.0.0.1:8787`。

常用启动参数：

```bash
flowith-claude-proxy --host 0.0.0.0 --port 8787
flowith-claude-proxy --reload          # 开发模式，自动重启
flowith-claude-proxy --version
```

启动成功后你会看到类似输出：

```
Flowith Claude-Compatible Proxy v0.1.0
  Listening:    http://127.0.0.1:8787
  Upstream:     https://edge.flowith.io/external/use/llm
  Endpoint:     POST http://127.0.0.1:8787/v1/messages
```

暴露的接口：

| 方法 | 路径           | 说明                       |
| ---- | -------------- | -------------------------- |
| GET  | `/`            | 服务信息                   |
| GET  | `/health`      | 健康检查                   |
| POST | `/v1/messages` | Claude 兼容的消息接口（主接口） |

---

## 五、客户端接入示例

### 1) Claude Code / Anthropic 官方 SDK

设置两个环境变量即可让它们走本代理：

Linux / macOS：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=你的Flowith_Key
claude   # 或运行任何 Anthropic SDK
```

Windows PowerShell：

```powershell
$env:ANTHROPIC_BASE_URL = "http://127.0.0.1:8787"
$env:ANTHROPIC_API_KEY  = "你的Flowith_Key"
claude
```

Windows CMD：

```cmd
set ANTHROPIC_BASE_URL=http://127.0.0.1:8787
set ANTHROPIC_API_KEY=你的Flowith_Key
claude
```

### 2) cc-switch

在 cc-switch 中新增一个端点：

| 字段     | 填写值                                                            |
| -------- | ----------------------------------------------------------------- |
| Base URL | `http://127.0.0.1:8787`                                           |
| API Key  | 你的 Flowith Key（若服务端已设 `FLOWITH_API_KEY`，这里随意填即可） |
| Model    | 如 `claude-3-5-sonnet-20241022`（会被映射）或 `gpt-5.4`（原样透传） |

### 3) curl 直接调用

非流式：

```bash
curl http://127.0.0.1:8787/v1/messages ^
  -H "content-type: application/json" ^
  -H "x-api-key: %FLOWITH_API_KEY%" ^
  -H "anthropic-version: 2023-06-01" ^
  -d "{\"model\":\"claude-3-5-sonnet-20241022\",\"max_tokens\":256,\"messages\":[{\"role\":\"user\",\"content\":\"用一句话和我打招呼\"}]}"
```

流式（SSE）：

```bash
curl -N http://127.0.0.1:8787/v1/messages ^
  -H "content-type: application/json" ^
  -H "x-api-key: %FLOWITH_API_KEY%" ^
  -H "anthropic-version: 2023-06-01" ^
  -d "{\"model\":\"claude-3-5-sonnet-20241022\",\"max_tokens\":256,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"从 1 数到 5\"}]}"
```

> Linux/macOS 把 `^` 续行符换成 `\`，并把 `%VAR%` 换成 `$VAR`。

---

## 六、模型映射

传入的 Claude 模型名会被自动映射到 Flowith 模型；非 Claude 命名（如 `gpt-5.4`、`gemini-2.5-pro`）会**原样透传**。

| 客户端传入的模型名                                | → 实际调用的 Flowith 模型 |
| ------------------------------------------------- | ------------------------- |
| `claude-3-5-sonnet-*`                             | `claude-4.6-sonnet`       |
| `claude-3-7-sonnet-*`                             | `claude-4.6-sonnet`       |
| `claude-sonnet-4-*` / `claude-sonnet-4-5`         | `claude-4.6-sonnet`       |
| `claude-opus-4-*` / `claude-3-opus-*`             | `claude-opus-4.7`         |
| `claude-3-haiku-*`                                | `claude-4.6-sonnet`       |
| `gpt-5.4`、`gpt-5.5`、`gemini-2.5-pro` 等         | 原样透传                  |

Flowith 当前可用的原生模型 id 包括：`gpt-5.5`、`gpt-5.4`、`claude-opus-4.7`、`claude-4.6-sonnet`、`gemini-2.5-pro`。

想自定义映射，编辑 `flowith_claude_proxy/adapter.py` 中的 `MODEL_ALIASES` 字典即可。

---

## 七、协议兼容性

本代理输出**严格符合** Anthropic 协议：

- 非流式响应字段完整：`id` / `type` / `role` / `model` / `content` / `stop_reason` / `stop_sequence` / `usage`
- `usage` 含 `input_tokens`、`output_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`
- SSE 事件按官方顺序发送：
  `message_start` → `ping` → `content_block_start` → `content_block_delta*` → `content_block_stop` → `message_delta` → `message_stop`

---

## 八、已知限制

- **工具调用 / Function Calling**：会被压平为纯文本（`[tool_use ...]` / `[tool_result ...]`），Flowith 暂不原生支持 Anthropic 的工具协议。
- **图片**输入会被丢弃，仅保留占位符 `[image omitted]`。
- 请求中的 `max_tokens` / `temperature` / `top_p` / `stop_sequences` 当前**未透传**给 Flowith。
- `usage` 中的 token 数来自 Flowith，cache 相关计数恒为 0。

---

## 九、常见问题

**Q1：启动时提示 `FLOWITH_API_KEY is not set on the server`？**
A：表示服务端没有兜底 Key。客户端只要在请求头里带 `x-api-key` 即可正常工作；如果想让服务端自带 Key，把 `.env` 配好或设置环境变量。

**Q2：返回 401 Missing API key？**
A：请求头里没有 `x-api-key` / `Authorization: Bearer`，且服务端也没有 `FLOWITH_API_KEY`。任选其一补上。

**Q3：返回 502 upstream_error？**
A：Flowith 上游调用失败。检查：① Key 是否有效；② 模型 id 是否为 Flowith 支持的；③ 网络能否访问 `edge.flowith.io`。

**Q4：怎么让局域网其他机器访问？**
A：用 `--host 0.0.0.0` 启动，并把客户端的 `ANTHROPIC_BASE_URL` 改成 `http://<本机IP>:8787`。

**Q5：端口被占用？**
A：用 `--port 其他端口` 启动，或在 `.env` 里设置 `FLOWITH_API_PORT`。

---

## 十、卸载

```bash
pip uninstall flowith-claude-proxy
```

---

## 许可证

MIT
