#!/usr/bin/env bash
# ============================================================================
# 政务智能体安全防护系统 — 端到端一键运行
#
# 用法:
#   ./run_e2e.sh check     # 环境检查 (依赖/密钥/端口)
#   ./run_e2e.sh start     # 启动 mock 工具服务(4001) + 防护代理(4000)
#   ./run_e2e.sh observe   # 同 start, 但代理为观察模式(不拦截, 测攻击有效性基线)
#   ./run_e2e.sh smoke     # 良性冒烟: 验证 LLM + 代理 + mock 全链路
#   ./run_e2e.sh bench     # 66 条真实有效攻击过有防护 OpenClaw
#   ./run_e2e.sh baseline  # 66 条过无防护 OpenClaw (需先以 observe 模式 start)
#   ./run_e2e.sh stop      # 停止服务
#
# 前置:
#   1. cp .env.example .env.local 并填入 API key (见 .env.example 注释)
#   2. OpenClaw 已安装在 OPENCLAW_HOME (默认 ../openclaw-runtime)
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/logs"; mkdir -p "$LOG_DIR"
MOCK_PORT=4001; PROXY_PORT=4000
OPENCLAW_HOME="${OPENCLAW_HOME:-$(dirname "$ROOT")/openclaw-runtime}"
OPENCLAW_BIN="$OPENCLAW_HOME/node_modules/.bin/openclaw"

say() { printf '\033[1;32m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[1;31m[ERR]\033[0m %s\n' "$*" >&2; exit 1; }

port_busy() { lsof -i :"$1" -sTCP:LISTEN >/dev/null 2>&1; }

cmd="${1:-help}"
case "$cmd" in
check)
  say "环境检查"
  python3 --version || die "需要 python3"
  [ -f "$ROOT/.env.local" ] || die "缺少 .env.local (cp .env.example .env.local 后填 key)"
  # 两家上游任填一家即可: PROXY_MODEL 名字里带 deepseek 走 deepseek, 否则走 stepfun
  grep -qE "^(DEEPSEEK_API_KEY|STEPFUN_API_KEY)=..*" "$ROOT/.env.local" \
    || die ".env.local 里 DEEPSEEK_API_KEY / STEPFUN_API_KEY 至少填一个"
  model=$(grep -E "^PROXY_MODEL=" "$ROOT/.env.local" | cut -d= -f2- | tr -d ' ')
  say "上游模型: ${model:-step-3.7-flash}"
  [ -x "$OPENCLAW_BIN" ] || die "未找到 OpenClaw: $OPENCLAW_BIN (设 OPENCLAW_HOME)"
  port_busy $MOCK_PORT && say "端口 $MOCK_PORT 已被占用 (mock 可能已在跑)" || say "端口 $MOCK_PORT 空闲"
  port_busy $PROXY_PORT && say "端口 $PROXY_PORT 已被占用 (proxy 可能已在跑)" || say "端口 $PROXY_PORT 空闲"
  say "检查通过"
  ;;

start|observe)
  [ -f "$ROOT/.env.local" ] || die "先 cp .env.example .env.local"
  port_busy $MOCK_PORT || {
    say "启动 mock 工具服务 :$MOCK_PORT"
    (cd "$ROOT" && nohup python3 -u src/scripts/mock_tool_server_v2.py --port $MOCK_PORT \
      > "$LOG_DIR/mock.log" 2>&1 &)
    sleep 1
  }
  port_busy $PROXY_PORT || {
    if [ "$cmd" = observe ]; then
      say "启动防护代理 :$PROXY_PORT (观察模式, 不拦截)"
      (cd "$ROOT" && GOV_PROXY_OBSERVE=1 nohup python3 -u src/system/proxy/gov_proxy.py \
        > "$LOG_DIR/proxy-observe.log" 2>&1 &)
    else
      say "启动防护代理 :$PROXY_PORT (执行模式)"
      (cd "$ROOT" && nohup python3 -u src/system/proxy/gov_proxy.py \
        > "$LOG_DIR/proxy.log" 2>&1 &)
    fi
    sleep 1
  }
  port_busy $MOCK_PORT && port_busy $PROXY_PORT && say "服务就绪: mock=$MOCK_PORT proxy=$PROXY_PORT" \
    || die "服务未就绪, 查 $LOG_DIR/"
  ;;

smoke)
  say "良性冒烟 (经代理调 LLM)"
  smoke_model=$(grep -E "^PROXY_MODEL=" "$ROOT/.env.local" 2>/dev/null | cut -d= -f2- | tr -d ' ')
  smoke_model="${smoke_model:-step-3.7-flash}"
  resp=$(curl -s -m 60 -X POST "http://127.0.0.1:$PROXY_PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$smoke_model\",\"messages\":[{\"role\":\"user\",\"content\":\"回复ok即可\"}],\"max_tokens\":200}")
  echo "$resp" | python3 -c "import json,sys; r=json.load(sys.stdin); print('  LLM 回复:', (r['choices'][0]['message'].get('content') or '(reasoning)')[:40])" \
    || die "冒烟失败: $resp"
  say "冒烟通过"
  ;;

bench|baseline)
  [ -x "$OPENCLAW_BIN" ] || die "未找到 OpenClaw"
  port_busy $MOCK_PORT && port_busy $PROXY_PORT || die "先 ./run_e2e.sh start (或 observe)"
  out="$ROOT/results/$cmd-$(date +%m%d-%H%M%S).jsonl"; mkdir -p "$ROOT/results"
  if [ "$cmd" = bench ]; then
    say "66 条真实有效攻击 × 有防护 OpenClaw → $out"
  else
    pgrep -f "gov_proxy.py" | head -1 | xargs -I{} ps -p {} -o command= | grep -q "OBSERVE=1" \
      || die "baseline 需要代理处于观察模式: ./run_e2e.sh stop && ./run_e2e.sh observe"
    say "66 条 × 无防护 OpenClaw (基线) → $out"
  fi
  (cd "$ROOT" && python3 -u src/scripts/openclaw_case_runner.py \
    --file data/system-design/e2e-pilot/v11_final_effective_pool.jsonl \
    --timeout 150 --out "$out")
  say "完成: $out"
  ;;

stop)
  say "停止服务"
  pkill -f "mock_tool_server_v2" 2>/dev/null || true
  pkill -f "gov_proxy.py" 2>/dev/null || true
  sleep 1
  say "已停止"
  ;;

*)
  sed -n '2,16p' "$0" | grep -E "^#|^=" | head -16
  ;;
esac
