# 快速开始

## 5 分钟上手指南

### 1️⃣ 安装依赖 (1 分钟)

**Windows**:
```cmd
cd claude-proxy
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**Linux/macOS**:
```bash
cd claude-proxy
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2️⃣ 配置环境 (2 分钟)

复制配置模板:
```bash
cp .env.example .env
```

编辑 `.env` 文件:
```env
UPSTREAM_API_KEY=sk-your-deepseek-key-here
UPSTREAM_BASE_URL=https://api.deepseek.com/v1
UPSTREAM_MODEL=deepseek-chat
```

**获取 API Key**:
- DeepSeek: https://platform.deepseek.com/
- OpenAI: https://platform.openai.com/
- 其他兼容服务

### 3️⃣ 启动服务 (30 秒)

**方式 1: 脚本启动**
```bash
# Windows
start.bat

# Linux/macOS
chmod +x start.sh
./start.sh
```

**方式 2: 直接运行**
```bash
python -m proxy
```

看到以下输出表示成功:
```
╔═══════════════════════════════════════╗
║     Claude API Proxy v1.0.0           ║
╚═══════════════════════════════════════╝

🚀 服务启动中...
📍 地址: http://127.0.0.1:8000
```

### 4️⃣ 测试连接 (30 秒)

打开浏览器访问:
```
http://127.0.0.1:8000/health
```

或使用命令:
```bash
curl http://127.0.0.1:8000/health
```

预期响应:
```json
{
  "status": "healthy",
  "upstream": "https://api.deepseek.com/v1",
  "multi_agent": true,
  "active_agents": 0,
  "queue_length": 0
}
```

### 5️⃣ 导入 CC Switch (1 分钟)

**方法 A: UI 导入**

1. 打开 CC Switch
2. 点击 "Add Provider" → "Custom Provider"
3. 填写:
   - Name: `Claude Proxy`
   - API Key: `sk-proxy-001` (任意)
   - Base URL: `http://127.0.0.1:8000`
   - Model: `claude-3-5-sonnet-20241022`
4. 保存并启用

**方法 B: Deep Link**

点击此链接(或复制到浏览器):
```
ccswitch://provider/import?name=Claude%20Proxy&apiKey=sk-proxy-001&baseUrl=http://127.0.0.1:8000&model=claude-3-5-sonnet-20241022
```

### 6️⃣ 开始使用

重启 Claude Code 终端,然后发送消息测试!

## 常见问题

### ❌ 端口被占用

**现象**: `Address already in use: 8000`

**解决**:
```bash
# 方法 1: 修改端口
# 编辑 .env: PROXY_PORT=8001

# 方法 2: 关闭占用进程
# Windows
netstat -ano | findstr :8000
taskkill /PID <PID> /F

# Linux/macOS
lsof -ti:8000 | xargs kill -9
```

### ❌ 上游连接失败

**现象**: `Connection refused` 或 `Timeout`

**检查**:
1. API Key 是否正确
2. Base URL 是否正确
3. 网络是否畅通
4. 是否需要代理(配置 `HTTP_PROXY`)

### ❌ Claude Code 无响应

**检查**:
1. Proxy 是否正在运行(`http://127.0.0.1:8000/health`)
2. CC Switch 配置是否正确
3. 是否重启了终端
4. 查看 Proxy 日志是否有请求

### ❌ 缺少依赖

**现象**: `ModuleNotFoundError: No module named 'fastapi'`

**解决**:
```bash
pip install -r requirements.txt
```

## 高级配置

### 多 Agent 配置

编辑 `.env`:
```env
ENABLE_MULTI_AGENT=true
MAX_AGENTS=5  # 增加并发数
```

### 代理配置

```env
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
```

### 调试模式

```env
LOG_LEVEL=DEBUG
DEBUG_MODE=true
```

### Docker 部署

```bash
# 构建镜像
docker build -t claude-proxy .

# 运行容器
docker run -d \
  --name claude-proxy \
  -p 8000:8000 \
  -e UPSTREAM_API_KEY=sk-your-key \
  -e UPSTREAM_BASE_URL=https://api.deepseek.com/v1 \
  claude-proxy

# 或使用 docker-compose
docker-compose up -d
```

## 测试脚本

运行完整测试:
```bash
python test_proxy.py
```

测试内容:
- ✅ 健康检查
- ✅ 非流式请求
- ✅ 流式请求

## 性能调优

### 并发优化

```env
MAX_AGENTS=10  # 根据机器配置调整
```

**建议**:
- 2 核 4G: `MAX_AGENTS=3`
- 4 核 8G: `MAX_AGENTS=5`
- 8 核 16G: `MAX_AGENTS=10`

### 超时配置

修改 `proxy/upstream.py`:
```python
self.client = httpx.AsyncClient(
    timeout=120.0,  # 增加超时时间
    ...
)
```

## API 文档

启动后访问:
```
http://127.0.0.1:8000/docs
```

交互式 API 文档(Swagger UI)

## 日志查看

实时查看日志:
```bash
# 直接运行时,日志输出到终端

# Docker 运行
docker logs -f claude-proxy

# 后台运行(重定向日志)
python -m proxy > proxy.log 2>&1 &
tail -f proxy.log
```

## 更新

拉取最新代码:
```bash
git pull
pip install -r requirements.txt --upgrade
```

重启服务:
```bash
# 停止服务(Ctrl+C)
# 重新运行
python -m proxy
```

## 下一步

- 📖 阅读 [CC_SWITCH_GUIDE.md](CC_SWITCH_GUIDE.md) 了解 CC Switch 详细配置
- 🏗️ 阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 了解架构设计
- 🔧 修改代码适配你的需求

## 技术支持

遇到问题?

1. 查看日志输出
2. 访问 `/health` 检查状态
3. 提交 Issue 或 PR

---

**提示**: 确保 `.env` 文件配置正确是成功的关键!
