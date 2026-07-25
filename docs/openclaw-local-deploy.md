# OpenClaw 本地部署指南（政务技能验证）

> 本文档说明如何在本地固定位置安装 OpenClaw，并部署比赛仓库的 50 个政务办公 SKILL，用于验证自研防护系统在真实 agent 下的拦截效果。

## 1. 部署位置

所有内容固定安装在：
```
/Users/mac/Documents/日常工作/openclaw-runtime/
├── node_modules/        # OpenClaw 2026.7.1-2
├── package.json
├── package-lock.json
├── .openclaw/            # OpenClaw 状态目录（OPENCLAW_HOME）
│   └── workspace/       # Agent workspace
│       └── skills/      # 50 个政务 SKILL（硬复制）
└── OPENCLAW-LOCAL-DEPLOY.md   # 本文档
```

**总大小**：约 386MB（OpenClaw 完整依赖）。

## 2. 系统要求

- macOS / Linux
- Node.js 22.22+ 或 24.15+ 或 25.9+ （推荐 24）
- npm 11+
- 至少 500MB 可用磁盘空间

## 3. 安装步骤

### 3.1 安装 OpenClaw（一次性）

```bash
mkdir -p /Users/mac/Documents/日常工作/openclaw-runtime
cd /Users/mac/Documents/日常工作/openclaw-runtime
npm init -y
npm install openclaw
# 安装约 386MB，约 1-2 分钟
```

### 3.2 部署政务 SKILL（与比赛仓库联动）

```bash
# 比赛仓库已有 50 个 SKILL（含 5 旧版 + 45 精修版）
SRC="/Users/mac/Documents/日常工作/student_competition/data/block-03-gov-original-skills/skills"

# OpenClaw 不允许软链接跳出 workspace 根目录，必须硬复制
mkdir -p "$(pwd)/.openclaw/workspace/skills"
for skill_dir in "$SRC"/*/; do
    skill_name=$(basename "$skill_dir")
    if [ -f "$skill_dir/SKILL.md" ]; then
        mkdir -p ".openclaw/workspace/skills/$skill_name"
        git -C /Users/mac/Documents/日常工作/student_competition \
            show "HEAD:data/block-03-gov-original-skills/skills/$skill_name/SKILL.md" \
            > ".openclaw/workspace/skills/$skill_name/SKILL.md"
        [ -f "$skill_dir/MAPPING.md" ] && cp "$skill_dir/MAPPING.md" \
                                          ".openclaw/workspace/skills/$skill_name/MAPPING.md"
    fi
done
# 预期: 50 个 SKILL
ls .openclaw/workspace/skills/ | wc -l
```

### 3.3 设置环境变量

在 `~/.zshrc` 或 `~/.bashrc` 添加：
```bash
export OPENCLAW_HOME="/Users/mac/Documents/日常工作/openclaw-runtime"
export OPENCLAW_STATE_DIR="$OPENCLAW_HOME/.openclaw"
```

### 3.4 配置 LLM API key

根据你的厂商选一种：

```bash
# OpenAI
node_modules/.bin/openclaw config set model.providers.openai.apiKey "sk-..."
node_modules/.bin/openclaw config set model.default "openai/gpt-4o"

# Anthropic
node_modules/.bin/openclaw config set model.providers.anthropic.apiKey "sk-ant-..."
node_modules/.bin/openclaw config set model.default "anthropic/claude-3.5-sonnet"

# 或国内厂商（以 DeepSeek 为例）
node_modules/.bin/openclaw config set model.providers.deepseek.apiKey "sk-..."
node_modules/.bin/openclaw config set model.default "deepseek/deepseek-chat"
```

### 3.5 配置 gateway 模式

```bash
node_modules/.bin/openclaw config set gateway.mode local
node_modules/.bin/openclaw gateway --help  # 查看如何启动
```

## 4. 验证安装

```bash
export OPENCLAW_HOME="/Users/mac/Documents/日常工作/openclaw-runtime"

# 应该看到: Skills (69/107 ready) 含 50 个 gov-*
node_modules/.bin/openclaw skills list | grep gov- | wc -l
# 预期: 50

# 抽查单条
node_modules/.bin/openclaw skills info gov-doc-draft-assistant
# 预期: "✓ Ready", Visible to model: yes
```

## 5. 端到端验证（可选）

启动 gateway 后用 mock agent 跑一条政务办公任务，验证 SKILL 真能调用：

```bash
# 启动 gateway
node_modules/.bin/openclaw gateway start

# 另开终端，发起测试会话
node_modules/.bin/openclaw chat <<'EOF'
请调用 gov-doc-draft-assistant 起草一份请示，主题申请增加办公设备
EOF
```

期望：能看到 OpenClaw 加载 gov-doc-draft-assistant 的 SKILL.md，按其定义的公文格式返回草稿。

## 6. 与自研防护系统集成

OpenClaw 的 tool call 可以挂载到我们的自研防护系统（`src/system/`）做实时拦截：

- 方式 A：把 OpenClaw 的 base URL 指向 ArbiterOS 的 OpenAI-compatible endpoint
  （ArbiterOS 是 OpenClaw 原生支持的拦截层）
- 方式 B：在 OpenClaw hook 里调用我们 `src/system/runner/harness.py` 的策略引擎
- 方式 C：直接读 OpenClaw 的 trace 日志离线回放（最简）

后续 Phase 4 演示时再实施。

## 7. 卸载 / 删除

完全删除（**这是用独立目录安装的最大好处**）：

```bash
rm -rf /Users/mac/Documents/日常工作/openclaw-runtime
```

OpenClaw、skills、状态、配置**全部清干净**，不影响比赛仓库。

## 8. 故障排查

| 问题 | 解决 |
|------|------|
| `Skills (0/107 ready)` | 检查 `OPENCLAW_HOME` 环境变量是否设置；检查 `.openclaw/workspace/skills/` 下是否有 SKILL.md |
| 软链接报错 `symlink-escape` | 必须硬复制，不能用 `ln -s` |
| `✓ ready` 后还显示 `needs setup` | skill 引用了 CLI 工具（如 `gh`），需要先安装对应 CLI |
| gateway 启动失败 | 检查 `gateway.mode` 是否设为 `local` |

## 9. 部署检查清单

- [ ] Node.js 22+ 已装
- [ ] `cd /Users/mac/Documents/日常工作/openclaw-runtime && ls node_modules/.bin/openclaw` 存在
- [ ] `.openclaw/workspace/skills/` 下有 50 个 SKILL.md
- [ ] `openclaw skills list | grep gov- | wc -l` = 50
- [ ] LLM API key 已配（针对你的厂商）
- [ ] `openclaw config get model.default` 能看到默认模型
