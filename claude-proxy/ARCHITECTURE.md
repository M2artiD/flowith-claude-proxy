# 架构设计

## 概览

```
┌─────────────────────────────────────────────────────────────┐
│                    Claude Code 客户端                        │
│              (Anthropic Messages API 格式)                   │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                  Claude Proxy (FastAPI)                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │   API 层     │  │  转换层      │  │  多 Agent 路由   │  │
│  │ /v1/messages │→ │ Claude↔OpenAI│→ │   任务分配       │  │
│  │   /health    │  │   格式转换   │  │   优先级队列     │  │
│  └──────────────┘  └──────────────┘  └──────────────────┘  │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                   上游 LLM 服务                              │
│        (OpenAI API 兼容 - DeepSeek/GPT/etc.)                │
└─────────────────────────────────────────────────────────────┘
```

## 核心模块

### 1. API 层 (`server.py`)

**职责**: FastAPI 路由处理

- `POST /v1/messages` - 完整兼容 Anthropic Messages API
- `GET /health` - 健康检查
- `GET /` - 服务信息

**特性**:
- 流式 SSE 响应
- 非流式 JSON 响应
- CORS 支持
- 异常处理

### 2. 格式转换层 (`converter.py`)

**职责**: Anthropic ↔ OpenAI 格式互转

**转换映射**:

| Anthropic | OpenAI | 说明 |
|-----------|--------|------|
| `messages` | `messages` | 消息数组 |
| `system` | `messages[0]` (role=system) | 系统提示 |
| `max_tokens` | `max_tokens` | 最大 tokens |
| `temperature` | `temperature` | 温度 |
| `tools` | `tools` (function) | 工具定义 |
| `content_block` (text) | `message.content` | 文本内容 |
| `content_block` (tool_use) | `message.tool_calls` | 工具调用 |

**stop_reason 映射**:

| OpenAI finish_reason | Claude stop_reason |
|----------------------|--------------------|
| `stop` | `end_turn` |
| `length` | `max_tokens` |
| `tool_calls` | `tool_use` |
| `content_filter` | `stop_sequence` |

### 3. 多 Agent 路由 (`multi_agent.py`)

**职责**: 任务分配与队列管理

**组件**:

- `AgentTask` - 任务对象
  - `task_id` - 任务 ID
  - `request` - 请求数据
  - `priority` - 优先级
  - `status` - 状态 (pending/processing/completed/failed)

- `MultiAgentRouter` - 路由器
  - `analyze_task_complexity()` - 分析任务复杂度
  - `should_use_multi_agent()` - 判断是否需要多 Agent
  - `assign_agent()` - 分配 Agent
  - `release_agent()` - 释放 Agent
  - `get_next_task()` - 获取下一个任务

**优先级计算**:
```python
priority = 0
if len(content) > 10000: priority += 2
elif len(content) > 5000: priority += 1
if has_tools: priority += 2
if is_stream: priority -= 1
```

**触发条件**:
- 消息总长度 > 1000 字符
- 工具数量 > 3
- 消息数 > 10 条

### 4. 上游客户端 (`upstream.py`)

**职责**: 调用上游 OpenAI 兼容 API

**方法**:
- `chat_completion()` - 非流式调用
- `chat_completion_stream()` - 流式调用

**特性**:
- HTTP/HTTPS 代理支持
- 超时控制 (60s)
- 自动重定向
- 异常处理

### 5. 配置管理 (`config.py`)

**职责**: 环境变量加载

**配置项**:
```python
proxy_host: str = "127.0.0.1"
proxy_port: int = 8000
upstream_api_key: str
upstream_base_url: str
upstream_model: str
http_proxy: str = ""
https_proxy: str = ""
enable_multi_agent: bool = True
max_agents: int = 3
log_level: str = "INFO"
debug_mode: bool = False
```

### 6. 数据模型 (`models.py`)

**Pydantic 模型**:
- `Message` - 消息
- `ClaudeRequest` - 请求
- `ClaudeResponse` - 响应
- `ContentBlock` - 内容块
- `Usage` - Token 统计
- `StreamEvent` - SSE 事件

## 流式响应流程

### 1. 非流式

```
客户端请求
  ↓
格式转换 (Claude → OpenAI)
  ↓
多 Agent 判断 & 分配
  ↓
调用上游 (一次性获取完整响应)
  ↓
格式转换 (OpenAI → Claude)
  ↓
返回 JSON
```

### 2. 流式 SSE

```
客户端请求 (stream=true)
  ↓
格式转换 (Claude → OpenAI)
  ↓
建立 SSE 连接
  ↓
发送 message_start 事件
  ↓
发送 content_block_start 事件
  ↓
循环接收上游流式块
  ├─ 解析 delta.content
  ├─ 构造 content_block_delta 事件
  └─ yield SSE 格式数据
  ↓
发送 content_block_stop 事件
  ↓
发送 message_delta 事件 (stop_reason)
  ↓
发送 message_stop 事件
```

### SSE 事件顺序

```
event: message_start
data: {"type":"message_start","message":{...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{...}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{...}}

[... 多个 delta 事件 ...]

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},...}

event: message_stop
data: {"type":"message_stop"}
```

## 多 Agent 协作机制

### 场景示例

**场景 1: 简单对话**
- 消息长度 < 1000
- 无工具调用
- **结果**: 单 Agent 直接处理,无队列

**场景 2: 复杂代码生成**
- 消息长度 > 5000
- 有 5 个工具 (Read/Write/Execute)
- **结果**: 多 Agent 模式,优先级 +4

**场景 3: 大量并发**
- 同时收到 5 个请求
- `max_agents = 3`
- **结果**: 
  - 前 3 个立即分配 Agent
  - 后 2 个进入优先级队列
  - 按优先级依次处理

### 队列管理

```python
# 任务加入队列
task = AgentTask(task_id, request, priority=4)
await agent_router.assign_agent(task)

# 如果 active_agents >= max_agents
# → task 加入 task_queue
# → 按 priority 排序

# Agent 释放后
await agent_router.release_agent(task_id)
# → 自动从队列取出下一个高优先级任务
```

## CC Switch 集成原理

### 配置写入

CC Switch 通过修改 Claude Code 的配置文件实现切换:

**Claude Code 配置路径**:
- Windows: `%APPDATA%\Code\User\globalStorage\...`
- macOS: `~/Library/Application Support/Code/User/globalStorage/...`
- Linux: `~/.config/Code/User/globalStorage/...`

**配置格式**:
```json
{
  "baseUrl": "http://127.0.0.1:8000",
  "apiKey": "sk-proxy-local-001",
  "model": "claude-3-5-sonnet-20241022"
}
```

### 兼容性

| 字段 | 必填 | 说明 |
|------|------|------|
| `baseUrl` | 是 | 不含 `/v1/messages`,仅到根路径 |
| `apiKey` | 是 | 任意字符串,Proxy 不验证 |
| `model` | 否 | 模型名称,用于日志和标识 |

### 验证流程

1. CC Switch 写入配置
2. 重启 Claude Code 终端
3. Claude Code 发送请求到 `http://127.0.0.1:8000/v1/messages`
4. Proxy 接收并转发到上游
5. 返回响应

## 性能优化

### 1. 异步 I/O

- FastAPI + `async/await`
- `httpx.AsyncClient` 异步 HTTP
- 流式处理减少内存占用

### 2. 连接复用

```python
# 全局单例客户端
upstream_client = UpstreamClient()

# 复用连接池
self.client = httpx.AsyncClient(...)
```

### 3. 任务队列

- 优先级队列避免低优先级任务阻塞
- Agent 池限制并发数,防止过载

### 4. 日志分级

- INFO: 关键操作日志
- DEBUG: 详细数据(仅开发时)
- 生产环境关闭 DEBUG

## 安全考虑

### 1. API Key 不验证

当前版本 Proxy 不验证 API Key,仅转发到上游。

**建议**: 
- 仅监听 `127.0.0.1`,不暴露公网
- 或添加中间件验证 `x-api-key` header

### 2. CORS

当前允许所有源 (`allow_origins=["*"]`),适合本地开发。

**生产环境**: 限制为特定域名

### 3. 日志脱敏

不要记录敏感信息 (API Key, 完整请求内容)

## 扩展方向

### 1. 多上游负载均衡

```python
upstreams = [
    {"url": "https://api.deepseek.com/v1", "weight": 70},
    {"url": "https://api.openai.com/v1", "weight": 30}
]
```

### 2. 请求缓存

- 缓存重复请求的响应
- 使用 Redis 或内存缓存

### 3. 速率限制

```python
from slowapi import Limiter

limiter = Limiter(key_func=get_remote_address)

@app.post("/v1/messages")
@limiter.limit("10/minute")
async def create_message(...):
    ...
```

### 4. 监控指标

- Prometheus metrics
- 请求延迟、错误率、Token 消耗

### 5. Agent 智能调度

- 根据模型类型分配不同 Agent
- 动态调整 `max_agents`

---

**设计原则**:
- ✅ 简洁 - 核心文件 < 10 个,每个 < 300 行
- ✅ 模块化 - 职责清晰,易于扩展
- ✅ 兼容性 - 严格遵循 Anthropic API 规范
- ✅ 可观测 - 详细日志,健康检查
