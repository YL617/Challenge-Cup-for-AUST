#!/usr/bin/env bash
# scripts/openclaw_chat.sh — 通过防护代理向 OpenClaw agent 发一条指令
#
# 前置: 先运行 bash scripts/run_demo.sh (proxy 必须在 :4000 监听)
# 用法: bash scripts/openclaw_chat.sh "你的指令"
#
# 拦截可见性:
#   - 本脚本输出 agent 的回复 (被拦后 agent 会告知无法执行)
#   - proxy 终端 (run_demo.sh 所在终端) 实时打印 🚫 BLOCKED 记录
#   - 结构化审计: src/system/proxy/audit_log.jsonl

set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

MSG="${1:-}"
if [ -z "$MSG" ]; then
    echo "用法: bash scripts/openclaw_chat.sh \"你的指令\""
    exit 1
fi

OPENCLAW_RUNTIME="${OPENCLAW_RUNTIME:-$HOME/Documents/日常工作/openclaw-runtime}"
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/tmp/openclaw-spike-state}"

if [ ! -x "$OPENCLAW_RUNTIME/node_modules/.bin/openclaw" ]; then
    echo "✗ 未找到 OpenClaw: $OPENCLAW_RUNTIME/node_modules/.bin/openclaw"
    echo "  请先运行一次性安装: bash scripts/openclaw_setup.sh"
    exit 1
fi

if [ ! -f "$OPENCLAW_STATE_DIR/openclaw.json" ]; then
    echo "✗ OpenClaw 演示环境未初始化: $OPENCLAW_STATE_DIR"
    echo "  请先运行一次性安装: bash scripts/openclaw_setup.sh"
    exit 1
fi

# proxy 存活检查
if ! lsof -ti:4000 >/dev/null 2>&1; then
    echo "✗ 防护代理未启动 (:4000 无监听)"
    echo "  请先在另一个终端运行: bash scripts/run_demo.sh"
    exit 1
fi

# 加载 API key (STEPFUN_API_KEY 供 OpenClaw 经 proxy 转发时上游使用)
set -a
# shellcheck disable=SC1091
. "$ROOT/.env.local"
set +a

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  发送指令 (经防护代理 :4000)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  $MSG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

cd "$OPENCLAW_RUNTIME"
RESULT=$(perl -e 'alarm 120; exec @ARGV' -- node_modules/.bin/openclaw agent --local \
    --agent main \
    --session-key "demo-$(date +%s)" \
    --model anthropic/step-3.7-flash \
    -m "$MSG" --json 2>&1) || true

# 提取可读回复
echo "$RESULT" | python3 -c '
import json, sys
raw = sys.stdin.read()
# openclaw --json 输出混有日志行, 目标 JSON 是含 payloads 的最外层对象。
# 从每个 { 位置尝试解析, 取第一个含 payloads 键的。
data = None
decoder = json.JSONDecoder()
pos = 0
while True:
    start = raw.find("{", pos)
    if start < 0:
        break
    try:
        cand, _end = decoder.raw_decode(raw, start)
        if isinstance(cand, dict) and ("payloads" in cand or "meta" in cand):
            data = cand
            break
    except json.JSONDecodeError:
        pass
    pos = start + 1
if not data:
    print("[agent 原始输出]")
    print(raw[:2000])
    sys.exit(0)
print("[agent 回复]")
payloads = data.get("payloads") or []
texts = [p.get("text", "") for p in payloads if p.get("text")]
if not texts:
    # 被拦截等场景回复可能在 meta.finalAssistantVisibleText
    meta = data.get("meta") or {}
    t = meta.get("finalAssistantVisibleText") or meta.get("finalAssistantRawText")
    if t:
        texts = [t]
if texts:
    for t in texts:
        print(t)
else:
    print(json.dumps(data, ensure_ascii=False, indent=2)[:1500])
print()
tools = data.get("toolSummary") or (data.get("meta") or {}).get("toolSummary") or []
if tools:
    print("[本次工具调用]")
    for ts in tools:
        print(f"  - {ts}")
'

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  拦截记录请查看:"
echo "  ① proxy 终端 (run_demo.sh 所在终端, 🚫 BLOCKED 行)"
echo "  ② tail -3 $ROOT/src/system/proxy/audit_log.jsonl"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
