#!/usr/bin/env bash
# scripts/openclaw_setup.sh — OpenClaw 演示环境一次性安装
#
# 做三件事:
#   1. 在 OPENCLAW_RUNTIME 安装 OpenClaw (如已装则跳过)
#   2. 初始化演示用 state 目录 (OPENCLAW_STATE_DIR), 复制 50 个政务 skill
#   3. 把 OpenClaw 的模型 baseUrl 指向防护代理 :4000
#
# 环境变量(可选覆盖):
#   OPENCLAW_RUNTIME   OpenClaw 安装目录, 默认 ~/Documents/日常工作/openclaw-runtime
#   OPENCLAW_STATE_DIR 演示 state 目录, 默认 /tmp/openclaw-spike-state

set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

OPENCLAW_RUNTIME="${OPENCLAW_RUNTIME:-$HOME/Documents/日常工作/openclaw-runtime}"
OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-spike-state}"
SKILLS_SRC="$ROOT/data/block-03-gov-original-skills/skills"

echo "========================================"
echo "  OpenClaw 演示环境一次性安装"
echo "========================================"
echo "  安装目录: $OPENCLAW_RUNTIME"
echo "  state 目录: $OPENCLAW_STATE_DIR"
echo ""

# 1. 安装 OpenClaw
echo "[1/3] 检查 OpenClaw 安装"
if [ -x "$OPENCLAW_RUNTIME/node_modules/.bin/openclaw" ]; then
    echo "  ✓ 已安装, 跳过"
else
    echo "  安装中 (约 386MB, 1-2 分钟)..."
    mkdir -p "$OPENCLAW_RUNTIME"
    cd "$OPENCLAW_RUNTIME"
    [ -f package.json ] || npm init -y
    npm install openclaw
    cd "$ROOT"
    echo "  ✓ 安装完成"
fi

# 2. 初始化 state + 复制 skills
echo ""
echo "[2/3] 初始化演示 state 目录与政务 skill"
mkdir -p "$OPENCLAW_STATE_DIR/workspace/skills"
if [ -d "$SKILLS_SRC" ]; then
    count=0
    for skill_dir in "$SKILLS_SRC"/*/; do
        skill_name=$(basename "$skill_dir")
        if [ -f "$skill_dir/SKILL.md" ]; then
            mkdir -p "$OPENCLAW_STATE_DIR/workspace/skills/$skill_name"
            cp "$skill_dir/SKILL.md" "$OPENCLAW_STATE_DIR/workspace/skills/$skill_name/SKILL.md"
            [ -f "$skill_dir/MAPPING.md" ] && cp "$skill_dir/MAPPING.md" "$OPENCLAW_STATE_DIR/workspace/skills/$skill_name/MAPPING.md"
            count=$((count + 1))
        fi
    done
    echo "  ✓ 已复制 $count 个政务 skill 到 workspace/skills/"
else
    echo "  ✗ 未找到 skill 源目录: $SKILLS_SRC"
    exit 1
fi

# 3. baseUrl 指向防护代理
echo ""
echo "[3/3] 配置 OpenClaw 模型走防护代理"
cd "$OPENCLAW_RUNTIME"
OPENCLAW_STATE_DIR="$OPENCLAW_STATE_DIR" node_modules/.bin/openclaw config patch --stdin <<'EOF'
{
  "models": {
    "providers": {
      "anthropic": {
        "baseUrl": "http://127.0.0.1:4000",
        "api": "openai-completions",
        "authHeader": false,
        "models": [
          {"id": "step-3.7-flash", "name": "step-3.7-flash", "input": ["text"], "contextWindow": 128000}
        ]
      }
    }
  },
  "agents": {"defaults": {"model": {"primary": "anthropic/step-3.7-flash"}}}
}
EOF
cd "$ROOT"
echo "  ✓ baseUrl → http://127.0.0.1:4000 (防护代理)"

echo ""
echo "========================================"
echo "  安装完成! 验证:"
echo "========================================"
echo "  1. bash scripts/run_demo.sh          # 终端 1: 启动防护服务"
echo "  2. bash scripts/openclaw_chat.sh \"你好, 介绍一下你自己\"   # 终端 2: 试一条"
echo ""
