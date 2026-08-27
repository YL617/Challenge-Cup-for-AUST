#!/usr/bin/env bash
# G5 全量复跑 (过夜批次): 分层防护定版数字
#   1) 66 条真实有效攻击 × 有防护 OpenClaw × 3 seeds
#   2) 白名单 25 条 × 仿真 runner(经代理) × 3 seeds
#   3) 66 条 × 无防护 OpenClaw (observe 基线) × 1
# 产出: results/g5-*.jsonl
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="results"; mkdir -p "$OUT"
POOL="data/system-design/e2e-pilot/v11_final_effective_pool.jsonl"
WHITE="data/system-design/e2e-pilot/white_cases_v1_25.jsonl"
ts() { date +%H:%M:%S; }

echo "[$(ts)] G5 复跑启动 (确保服务就绪, 代理为分层新版执行模式)"
./run_e2e.sh stop >/dev/null 2>&1 || true
sleep 1
./run_e2e.sh start || { echo "服务启动失败"; exit 1; }
./run_e2e.sh smoke >/dev/null || { echo "冒烟失败"; exit 1; }

for r in 1 2 3; do
  echo "[$(ts)] === 有防护 66 条 seed $r ==="
  OPENCLAW_HOME="${OPENCLAW_HOME:-$(dirname "$ROOT")/openclaw-runtime}" python3 -u src/scripts/openclaw_case_runner.py --file "$POOL" --timeout 150 \
    --out "$OUT/g5-guarded-r$r.jsonl" 2>&1 | tail -6
done

echo "[$(ts)] === 白名单 25 条 × 3 (runner 经代理) ==="
for r in 1 2 3; do
  set -a; source .env.local; set +a
  PROXY_URL="http://127.0.0.1:4000/v1/chat/completions" \
  MODEL_NAME="${PROXY_MODEL:-step-3.7-flash}" \
  python3 -u src/scripts/multiround_runner.py --cases "$WHITE" \
    --out "$OUT/g5-white-r$r.jsonl" --model "${PROXY_MODEL:-step-3.7-flash}" --white 2>&1 | tail -4
done

echo "[$(ts)] === 切观察模式, 无防护基线 66 条 × 1 ==="
pkill -f "gov_proxy.py" || true; sleep 1
GOV_PROXY_OBSERVE=1 nohup python3 -u src/system/proxy/gov_proxy.py > logs/proxy-observe-g5.log 2>&1 &
sleep 2
OPENCLAW_HOME="${OPENCLAW_HOME:-$(dirname "$ROOT")/openclaw-runtime}" python3 -u src/scripts/openclaw_case_runner.py --file "$POOL" --timeout 150 \
  --out "$OUT/g5-baseline-observe.jsonl" 2>&1 | tail -6

echo "[$(ts)] === 恢复执行模式 ==="
pkill -f "gov_proxy.py" || true; sleep 1
nohup python3 -u src/system/proxy/gov_proxy.py > logs/proxy.log 2>&1 &
sleep 2
echo "[$(ts)] G5 完成"
