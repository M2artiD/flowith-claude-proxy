# Claude Proxy 速查表

## 🚀 快速命令

```bash
# 安装
pip install -r requirements.txt

# 启动
python -m proxy                    # 直接启动
./start.sh                         # 脚本启动 (Linux/Mac)
start.bat                          # 脚本启动 (Windows)

# 测试
curl http://127.0.0.1:8000/health  # 健康检查
python test_proxy.py               # 完整测试

# Docker
docker-compose up -d               # 启动容器
docker logs -f claude-proxy        # 查看日志
docker-compose down                # 停止
```

## ⚙️ 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PROXY_HOST` | `127.0.0.1` | 监听地址 |
| `PROXY_PORT` | `8000` | 监听端口 |
| `UPSTREAM_API_KEY` | *必填* | 上游 API Key |
| `UPSTREAM_BASE_URL` | `https://api.deepseek.com/v1` | 上游地址 |
| `UPSTREAM_MODEL` | `deepseek-chat` | 模型名 |
| `HTTP_PROXY` | `` | HTTP 代理 |
| `HTTPS_PROXY` | `` | HTTPS 代理 |
| `ENABLE_MULTI_AGENT` | `true` | 启用多 Agent |
| `MAX_AGENTS` | `3` | 最大 Agent 数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |
| `DEBUG_MODE` | `false` | 调试模式 |

## 📡 API 端点

### GET /
服务信息
```bash
curl http://127.0.0.1:8000/
```

### GET /health
健康检查
```bash
curl http://127.0.0.1:8000/health
```

### POST /v1/messages
创建消息 (Anthropic 兼容)

**非流式**:
```bash
curl -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

**流式**:
```bash
curl -X POST http://127.0.0.1:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Count to 5"}
    ],
    "stream": true
  }'
```

## 🔌 CC Switch 配置

### 快速导入

**Deep Link**:
```
ccswitch://provider/import?name=Claude%20Proxy&apiKey=sk-proxy-001&baseUrl=http://127.0.0.1:8000&model=claude-3-5-sonnet-20241022
```

### 手动配置

| 字段 | 值 |
|------|-----|
| Name | `Claude Proxy` |
| API Key | `sk-proxy-001` (任意) |
| Base URL | `http://127.0.0.1:8000` |
| Model | `claude-3-5-sonnet-20241022` |

## 🐛 常见问题

### 端口被占用
```bash
# 修改 .env
PROXY_PORT=8001

# 或查找占用进程
# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Linux/Mac
lsof -ti:8000 | xargs kill -9
```

### 连接失败
```bash
# 检查服务状态
curl http://127.0.0.1:8000/health

# 检查日志
# 查看终端输出或 proxy.log
```

### API Key 错误
```bash
# 检查 .env 文件
cat .env | grep UPSTREAM_API_KEY

# 重新启动服务
python -m proxy
```

### 缺少依赖
```bash
pip install -r requirements.txt
```

## 📊 日志级别

| 级别 | 输出内容 |
|------|----------|
| `DEBUG` | 所有日志 (请求详情、响应数据) |
| `INFO` | 关键操作 (请求开始/完成、Agent 分配) |
| `WARNING` | 警告信息 (解析失败、超时) |
| `ERROR` | 错误信息 (请求失败、异常) |

修改 `.env`:
```env
LOG_LEVEL=DEBUG  # 调试时使用
```

## 🤖 多 Agent 调优

### 配置建议

| 机器配置 | MAX_AGENTS | 说明 |
|----------|------------|------|
| 2 核 4GB | `3` | 默认 |
| 4 核 8GB | `5` | 推荐 |
| 8 核 16GB | `10` | 高并发 |

### 触发条件

多 Agent 在以下情况自动触发:
- 消息总长度 > 1000 字符
- 工具数量 > 3
- 消息数 > 10 条

### 监控

查看当前 Agent 状态:
```bash
curl http://127.0.0.1:8000/health | jq
```

输出:
```json
{
  "status": "healthy",
  "active_agents": 2,
  "queue_length": 1
}
```

## 🔧 上游配置

### DeepSeek
```env
UPSTREAM_API_KEY=sk-your-deepseek-key
UPSTREAM_BASE_URL=https://api.deepseek.com/v1
UPSTREAM_MODEL=deepseek-chat
```

### OpenAI
```env
UPSTREAM_API_KEY=sk-your-openai-key
UPSTREAM_BASE_URL=https://api.openai.com/v1
UPSTREAM_MODEL=gpt-3.5-turbo
```

### 其他兼容服务
```env
UPSTREAM_API_KEY=your-api-key
UPSTREAM_BASE_URL=https://your-api.com/v1
UPSTREAM_MODEL=your-model
```

## 📦 Docker 速查

```bash
# 构建
docker build -t claude-proxy .

# 运行
docker run -d --name claude-proxy \
  -p 8000:8000 \
  -e UPSTREAM_API_KEY=sk-xxx \
  -e UPSTREAM_BASE_URL=https://api.deepseek.com/v1 \
  claude-proxy

# 日志
docker logs -f claude-proxy

# 停止
docker stop claude-proxy

# 删除
docker rm claude-proxy

# docker-compose
docker-compose up -d     # 启动
docker-compose down      # 停止
docker-compose logs -f   # 日志
docker-compose restart   # 重启
```

## 🧪 Python 客户端示例

### 非流式
```python
import httpx
import asyncio

async def test():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://127.0.0.1:8000/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": "你好"}
                ]
            }
        )
        print(response.json())

asyncio.run(test())
```

### 流式
```python
async def test_stream():
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            "http://127.0.0.1:8000/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "数到 5"}],
                "stream": True
            }
        ) as response:
            async for line in response.aiter_lines():
                if line.strip():
                    print(line)

asyncio.run(test_stream())
```

## 🔒 安全建议

1. **本地监听**: `PROXY_HOST=127.0.0.1` (不暴露公网)
2. **API Key 保护**: 不要提交 `.env` 到 Git
3. **日志脱敏**: 不记录 API Key 完整值
4. **CORS 限制**: 生产环境修改 `allow_origins`
5. **HTTPS**: 生产环境使用 HTTPS (Nginx 反代)

## 📚 文档链接

| 文档 | 用途 |
|------|------|
| [README.md](README.md) | 项目概览 |
| [QUICKSTART.md](QUICKSTART.md) | 快速上手 |
| [CC_SWITCH_GUIDE.md](CC_SWITCH_GUIDE.md) | CC Switch 集成 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构设计 |
| [COMPARISON.md](COMPARISON.md) | 项目对比 |

## 💡 提示

- 修改配置后需重启服务
- 查看日志定位问题
- 使用 `/health` 监控状态
- 调试时启用 `DEBUG_MODE`
- 生产环境关闭 `DEBUG_MODE`

---

**快速帮助**: `python -m proxy --help`
