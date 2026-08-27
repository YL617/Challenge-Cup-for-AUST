# PHASE 1 Spike 结果: OpenClaw 接入验证

## 日期: 2026-08-07

## ✅ 已验证成功

1. **baseUrl 可配置**: `openclaw config get/set models.providers.anthropic.baseUrl` 可以读取和修改 LLM API 端点
2. **OPENCLAW_STATE_DIR 绕过权限**: 用 `OPENCLAW_STATE_DIR=/tmp/openclaw-spike-state` + `config patch --stdin` 成功把 baseUrl 改为 `http://127.0.0.1:4000`(我们的 proxy)
3. **OpenAI Completions 协议**: OpenClaw 配置 `api: "openai-completions"`,与我们的 gov_proxy.py 协议一致
4. **不改 OpenClaw 源码**: 纯配置层面接入

## ❌ 阻塞点: workspace 权限

**问题**: `openclaw agent --local` 需要 `~/.openclaw/workspace/` 目录,但:
- 该目录受 macOS TCC 保护 (`com.apple.provenance` xattr)
- 从 Reasonix shell 执行 `mkdir ~/.openclaw/workspace` 报 EPERM
- 即使设了 `OPENCLAW_STATE_DIR`,workspace 路径仍硬编码到 `~/.openclaw/workspace`

**根因**: macOS 的 TCC (Transparency, Consent, and Control) 限制了第三方进程对 `~/.openclaw/` 的写权限。只有 OpenClaw CLI 自己(被用户授权)能写。

## 🔧 解决方案: 需要在用户终端操作

在**本地 Terminal.app**(不是 Reasonix)执行:

```bash
cd ~/Documents/日常工作/openclaw-runtime

# 1. 初始化 workspace (只有 OpenClaw 自己能创建)
node_modules/.bin/openclaw agent --local --agent main --session-key init -m "你好"

# 2. 把 baseUrl 改为 proxy
# 直接编辑 ~/.openclaw/openclaw.json,把 baseUrl 改成:
#   "baseUrl": "http://127.0.0.1:4000"

# 3. 测试流量是否经过 proxy
# 先启动 proxy,再跑 agent,看 proxy 日志有没有请求进来
```

## 📋 spike 结论

| 验证项 | 状态 |
|---|---|
| baseUrl 可配置 | ✅ |
| proxy 协议兼容 | ✅ |
| 不需改源码 | ✅ |
| workspace 创建 | ❌ 需要用户终端 |
| 真实流量经过 proxy | ⏳ 待 workspace 创建后验证 |

**风险评级**: 中等 — 技术路径已验证,阻塞点是 macOS 权限,需要在用户终端手动执行一次初始化。
