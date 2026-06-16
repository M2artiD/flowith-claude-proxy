# CC Switch 导入指南

本指南详细说明如何将 Claude Proxy 导入到 CC Switch 中使用。

## 前置要求

1. 已安装 CC Switch (推荐使用 Homebrew 或从 [GitHub Releases](https://github.com/farion1231/cc-switch/releases) 下载)
2. Claude Proxy 已启动并运行在 `http://127.0.0.1:8000`
3. 已安装 Claude Code 或 Codex

## 方法一: 通过 CC Switch UI 导入 (推荐)

### 步骤 1: 启动 Claude Proxy

```bash
cd claude-proxy
python -m proxy
```

确认服务启动成功,访问 http://127.0.0.1:8000/health 查看状态。

### 步骤 2: 打开 CC Switch

启动 CC Switch 桌面应用。

### 步骤 3: 添加自定义 Provider

1. 点击主界面的 **"Add Provider"** 按钮
2. 选择 **"Custom Provider"** (自定义提供商)
3. 填写以下信息:

   | 字段 | 值 | 说明 |
   |------|-----|------|
   | **Name** | `Claude Proxy` | 显示名称,可自定义 |
   | **API Key** | `sk-proxy-local-001` | 任意字符串即可 |
   | **Base URL** | `http://127.0.0.1:8000` | Proxy 地址 |
   | **Model** | `claude-3-5-sonnet-20241022` | 模型名称 |

4. (可选) 在 **"Advanced Settings"** 中配置:
   - Max Tokens: `4096`
   - Temperature: `1.0`

### 步骤 4: 启用 Provider

1. 保存配置后,在 Provider 列表中找到 `Claude Proxy`
2. 点击 **"Enable"** 按钮激活
3. 重启终端或 Claude Code (如果已打开)

### 步骤 5: 验证

打开 Claude Code,发送一条消息测试是否正常工作。

## 方法二: 通过 Deep Link 导入

CC Switch 支持通过 URL 快速导入配置。

### 导入链接

点击或复制以下链接到浏览器:

```
ccswitch://provider/import?name=Claude%20Proxy&apiKey=sk-proxy-local-001&baseUrl=http://127.0.0.1:8000&model=claude-3-5-sonnet-20241022
```

CC Switch 会自动打开并填充配置信息。

### 参数说明

| 参数 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | Provider 名称 |
| `apiKey` | 是 | API 密钥 |
| `baseUrl` | 是 | 代理服务地址 |
| `model` | 否 | 默认模型 |

## 方法三: 手动编辑配置 (高级)

如果你熟悉配置文件,可以直接编辑 CC Switch 的数据库或配置。

### 配置位置

- **数据库**: `~/.cc-switch/cc-switch.db` (SQLite)
- **配置**: `~/.cc-switch/settings.json`

### 示例 SQL 插入

```sql
INSERT INTO providers (name, api_key, base_url, model, active)
VALUES (
  'Claude Proxy',
  'sk-proxy-local-001',
  'http://127.0.0.1:8000',
  'claude-3-5-sonnet-20241022',
  0
);
```

**注意**: 手动编辑配置需要重启 CC Switch。

## 通用 Provider (跨工具同步)

如果你使用多个 AI 编程工具(Claude Code + Codex + Gemini CLI),可以创建 **Universal Provider**。

### 创建步骤

1. 在 CC Switch 中点击 **"Add Universal Provider"**
2. 填写配置(同上)
3. 选择要同步的工具:
   - ☑ Claude Code
   - ☑ Codex
   - ☑ Gemini CLI

4. 保存后,该配置会同步到所有选中的工具

## 系统托盘快速切换

CC Switch 支持从系统托盘快速切换 Provider,无需打开主应用。

### 使用方法

1. 点击系统托盘中的 CC Switch 图标
2. 在弹出菜单中选择 `Claude Proxy`
3. 立即生效(Claude Code 无需重启)

## 多环境配置

你可以为不同的使用场景创建多个 Provider:

| 场景 | Base URL | 说明 |
|------|----------|------|
| 本地开发 | `http://127.0.0.1:8000` | 本地测试 |
| 远程服务器 | `http://your-server.com:8000` | 团队共享 |
| 负载均衡 | `http://lb.example.com` | 生产环境 |

## 常见问题

### Q: 启用后 Claude Code 无响应?

**A**: 确认:
1. Proxy 服务是否正在运行 (`http://127.0.0.1:8000/health`)
2. 是否重启了终端/Claude Code
3. 查看 Proxy 日志是否有请求

### Q: 报错 "Connection refused"?

**A**: 检查:
1. Base URL 是否正确(不要多加 `/v1/messages`)
2. 端口是否被占用
3. 防火墙是否拦截

### Q: 如何切换回官方 Claude?

**A**:
1. 在 CC Switch 中添加 "Official Login" preset
2. 启用该 Provider
3. 重启 Claude Code 并重新登录

### Q: 支持多个 API Key 轮询吗?

**A**: 当前版本暂不支持,可以创建多个 Provider 手动切换。后续版本会添加自动轮询功能。

## 高级配置

### Shared Config Snippet

如果你的 Provider 有额外的配置(如 MCP 插件、Skills),可以使用 CC Switch 的 **Shared Config** 功能:

1. 编辑 Provider → 点击 **"Shared Config Panel"**
2. 点击 **"Extract from Current Provider"** 提取当前配置
3. 切换 Provider 时,勾选 **"Write Shared Config"** 自动注入

### 云同步

CC Switch 支持通过 Dropbox、OneDrive、iCloud 或 WebDAV 同步配置:

1. 设置 → **Cloud Sync**
2. 选择同步方式并配置路径
3. 所有设备的 Provider 配置自动同步

## 技术支持

- **GitHub Issues**: https://github.com/farion1231/cc-switch/issues
- **官方文档**: https://ccswitch.io
- **Proxy 项目**: (你的项目链接)

---

**提示**: 如果遇到问题,优先查看 Proxy 服务的日志输出,大部分问题都能从日志中找到原因。
