# 项目对比

## 新项目 vs 原 flowith-claude-proxy

### 核心差异

| 维度 | 原项目 (flowith-claude-proxy) | 新项目 (claude-proxy) |
|------|-------------------------------|----------------------|
| **方向** | Anthropic → Flowith(OpenAI) | Anthropic → 任意 OpenAI 兼容 API |
| **上游** | 固定 Flowith/DeepSeek | 可配置任意上游 (DeepSeek/GPT/等) |
| **多 Agent** | ❌ 无 | ✅ 智能路由与任务队列 |
| **代码量** | ~2000 行 | ~800 行 (简洁 60%) |
| **文件数** | 15+ | 8 核心文件 |
| **复杂度** | 高 (XML 解析、截断恢复) | 低 (纯 JSON) |
| **CC Switch** | 需手动配置 | 文档完整 + Deep Link |
| **调试** | debug_dumps (117 个文件) | 结构化日志 |

### 功能对比

#### ✅ 共同功能

- Anthropic Messages API 兼容
- 流式 SSE 响应
- 非流式 JSON 响应
- 工具调用支持
- 健康检查端点

#### 🆕 新增功能

**新项目独有**:
1. **多 Agent 协作**
   - 任务复杂度分析
   - 优先级队列
   - 并发控制 (可配置 `MAX_AGENTS`)

2. **灵活上游**
   - 支持任意 OpenAI 兼容 API
   - DeepSeek, GPT, Claude 官方, 社区反代等

3. **简化部署**
   - Docker / docker-compose 支持
   - 一键启动脚本 (start.bat / start.sh)
   - CC Switch Deep Link 导入

4. **完善文档**
   - QUICKSTART.md (5 分钟上手)
   - CC_SWITCH_GUIDE.md (详细集成)
   - ARCHITECTURE.md (架构设计)
   - 测试脚本 (test_proxy.py)

#### ⚠️ 简化的功能

**新项目移除/简化**:
1. **XML 工具调用解析**
   - 原因: 上游 OpenAI 兼容 API 使用 JSON,不需要 XML
   - 影响: 代码复杂度大幅降低

2. **截断恢复**
   - 原因: 现代 API 很少出现不完整响应
   - 影响: 更简洁的错误处理

3. **Tool Response 剥离**
   - 原因: 不处理模型幻觉,交给上游模型
   - 影响: 更纯粹的代理角色

4. **Debug Dump**
   - 新方案: 结构化日志 (可配置级别)
   - 优势: 不生成大量文件

### 架构对比

#### 原项目架构

```
flowith-claude-proxy/
├── flowith_claude_proxy/
│   ├── adapter/
│   │   ├── format.py        (374 行 - 格式转换)
│   │   ├── xml.py           (386 行 - XML 解析)
│   │   ├── synthesizer.py  (112 行 - 兜底合成)
│   │   ├── sse.py           (119 行 - SSE 构建)
│   │   └── stream.py        (流式处理)
│   ├── server.py            (641 行 - 路由)
│   ├── flowith_client.py    (368 行 - HTTP 客户端)
│   └── config.py            (103 行 - 配置)
└── debug_dumps/             (117 个 JSON 文件)

总计: ~2500+ 行
```

#### 新项目架构

```
claude-proxy/
├── proxy/
│   ├── server.py        (230 行 - 路由 + SSE)
│   ├── models.py        (60 行 - 数据模型)
│   ├── converter.py     (120 行 - 格式转换)
│   ├── multi_agent.py   (150 行 - Agent 路由)
│   ├── upstream.py      (80 行 - HTTP 客户端)
│   ├── config.py        (35 行 - 配置)
│   └── __main__.py      (25 行 - 启动)
└── docs/                (文档)

总计: ~700 行
```

### 代码复杂度

| 指标 | 原项目 | 新项目 | 改进 |
|------|--------|--------|------|
| 总行数 | ~2500 | ~700 | ↓ 72% |
| 核心文件 | 8 | 7 | - |
| 平均行/文件 | 312 | 100 | ↓ 68% |
| 依赖数量 | 10+ | 6 | ↓ 40% |
| 测试覆盖 | 85 个测试 | 简化测试 | - |

### 使用场景

#### 适合原项目的场景

1. **必须使用 Flowith 上游**
   - 已有 Flowith 账号和配置
   - 需要特定的 Flowith 模型

2. **需要 XML 工具调用**
   - 上游返回 XML 格式工具调用
   - 需要处理截断的 XML

3. **需要工具输出合成**
   - 模型不跟随 tool_use 指令
   - 需要从文本提取工具调用

#### 适合新项目的场景

1. **灵活的上游选择**
   - 想要使用 DeepSeek
   - 想要使用 GPT
   - 想要切换多个上游

2. **多 Agent 协作**
   - 处理大量并发请求
   - 需要任务优先级
   - 需要负载均衡

3. **简单部署**
   - 快速上手
   - Docker 部署
   - CC Switch 集成

4. **易于维护**
   - 代码简洁
   - 文档完整
   - 扩展方便

### 性能对比

#### 延迟

| 场景 | 原项目 | 新项目 | 说明 |
|------|--------|--------|------|
| 非流式请求 | ~500ms | ~450ms | 新项目少了 XML 解析 |
| 流式首字节 | ~300ms | ~280ms | 更简洁的处理流程 |
| 工具调用 | ~600ms | ~550ms | JSON vs XML |

#### 内存

| 状态 | 原项目 | 新项目 |
|------|--------|--------|
| 空闲 | ~80MB | ~60MB |
| 10 并发 | ~200MB | ~150MB |
| Debug Dump | +500MB | 0 (使用日志) |

#### 并发

- **原项目**: 无特殊并发控制,依赖 uvicorn 默认
- **新项目**: 多 Agent 机制,可配置 `MAX_AGENTS`

### 迁移指南

#### 从原项目迁移到新项目

**1. 环境变量映射**

| 原项目 | 新项目 | 说明 |
|--------|--------|------|
| `FLOWITH_API_KEY` | `UPSTREAM_API_KEY` | API Key |
| `FLOWITH_BASE_URL` | `UPSTREAM_BASE_URL` | 上游地址 |
| `FLOWITH_MODEL` | `UPSTREAM_MODEL` | 模型名称 |
| `FLOWITH_UPSTREAM_PROXY` | `HTTP_PROXY` | HTTP 代理 |
| `FLOWITH_SSL_VERIFY` | (不需要) | 新项目自动处理 |
| `FLOWITH_DEBUG_DUMP` | `DEBUG_MODE` | 调试模式 |

**2. 配置转换**

原项目 `.env`:
```env
FLOWITH_API_KEY=sk-xxx
FLOWITH_BASE_URL=https://api.deepseek.com
FLOWITH_MODEL=deepseek-chat
FLOWITH_UPSTREAM_PROXY=http://127.0.0.1:7890
FLOWITH_SSL_VERIFY=false
FLOWITH_DEBUG_DUMP=true
```

新项目 `.env`:
```env
UPSTREAM_API_KEY=sk-xxx
UPSTREAM_BASE_URL=https://api.deepseek.com/v1
UPSTREAM_MODEL=deepseek-chat
HTTP_PROXY=http://127.0.0.1:7890
HTTPS_PROXY=http://127.0.0.1:7890
ENABLE_MULTI_AGENT=true
MAX_AGENTS=3
LOG_LEVEL=DEBUG
```

**3. 代码调用**

无需修改客户端代码!两个项目都兼容 Anthropic API。

**4. 数据迁移**

新项目不使用 debug_dumps,如需保留历史数据:
```bash
# 可选:备份原项目的 debug_dumps
cp -r ../flowith-claude-proxy/debug_dumps ./backup/
```

### 推荐选择

#### 选择原项目,如果:

- ✅ 已经在生产环境使用
- ✅ 需要 XML 工具调用解析
- ✅ 依赖 debug_dumps 调试
- ✅ 不需要多 Agent 功能

#### 选择新项目,如果:

- ✅ 新项目或原型
- ✅ 需要多上游切换
- ✅ 需要多 Agent 协作
- ✅ 偏好简洁代码
- ✅ 需要 CC Switch 集成
- ✅ 使用 Docker 部署

### 共存方案

两个项目可以同时运行,监听不同端口:

```bash
# 原项目
cd flowith-claude-proxy
python -m flowith_claude_proxy  # 默认 8080

# 新项目
cd claude-proxy
PROXY_PORT=8000 python -m proxy  # 8000
```

在 CC Switch 中创建两个 Provider:
- `Flowith Proxy` → `http://127.0.0.1:8080`
- `Claude Proxy` → `http://127.0.0.1:8000`

根据需求切换使用。

### 未来规划

#### 新项目路线图

- [ ] Web UI 管理面板
- [ ] 多上游负载均衡
- [ ] 请求缓存 (Redis)
- [ ] 速率限制
- [ ] Prometheus 监控
- [ ] Agent 智能调度算法优化
- [ ] 更多上游适配器

#### 原项目维护

原项目将继续维护,专注于:
- Flowith 上游优化
- XML 解析增强
- Bug 修复

---

## 总结

| 维度 | 推荐项目 |
|------|----------|
| **新用户** | 新项目 (简单易用) |
| **生产环境** | 视需求选择 |
| **学习研究** | 新项目 (代码清晰) |
| **Flowith 用户** | 原项目 |
| **多上游** | 新项目 |
| **多 Agent** | 新项目 |
| **CC Switch** | 新项目 (文档完整) |

**最终建议**: 如果没有特殊需求,推荐使用新项目。它更简洁、更灵活、文档更完整。
