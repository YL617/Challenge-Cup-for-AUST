# PR 信息

## GitHub PR

- 仓库: https://github.com/YangYu-NUAA/Challenge-Cup-for-AUST
- 分支: feat/p0-real-e2e-test
- PR 链接: https://github.com/YangYu-NUAA/Challenge-Cup-for-AUST/pull/new/feat/p0-real-e2e-test

## Commits

```
f903486 chore: ignore 临时调试脚本 + proxy 审计日志
ebd10ce feat(p0): 真实 LLM 测试脚本 + deid 重生成脚本
b6aa76d feat(p0): 历史 case 工具脚本 (case 业务化 + 多版本演示套件)
fd959f5 feat(p0): 66 条真实 BYPASSED 攻击 case + 多轮 runner + proxy 拦截测试
```

## 合作学生本地测试方法

### 1. 拉分支

```bash
git clone https://github.com/YangYu-NUAA/Challenge-Cup-for-AUST.git
cd Challenge-Cup-for-AUST
git checkout feat/p0-real-e2e-test
git pull
```

### 2. 准备 API key

```bash
export DEEPSEEK_API_KEY=sk-你的key
# 或者用 stepfun
export STEPFUN_API_KEY=sk-你的key
```

### 3. 启动 mock tool server

```bash
python3 src/scripts/mock_tool_server_v2.py
# 启动后监听 4001
```

### 4. 跑测试

```bash
python3 src/scripts/multiround_runner.py \
    --model deepseek-v4-flash \
    --cases data/system-design/e2e-pilot/proxy_test_66.jsonl \
    --out my_test_results.jsonl
```

### 5. (可选) 启动防护 proxy

```bash
export STEPFUN_API_KEY=sk-xxx
python3 src/system/proxy/gov_proxy.py
# 启动后监听 4000
```

然后跑 proxy mode:
```bash
export PROXY_URL=http://127.0.0.1:4000
python3 src/scripts/multiround_runner.py \
    --model deepseek-v4-flash \
    --cases data/system-design/e2e-pilot/proxy_test_66.jsonl \
    --out my_proxy_results.jsonl
```

### 6. 期望结果

| 指标 | 无 proxy | 有 proxy |
|---|---|---|
| BYPASSED% | 50-70% | 50-90% (当前 proxy 弱) |
| REFUSED% | 10-30% | 10-30% |
| NO_ATTACK% | 20-40% | 20-40% |

## 文件结构

```
src/scripts/
├── multiround_runner.py          # 多轮 runner (支持 deepseek/stepfun/proxy)
├── mock_tool_server_v2.py        # 多轮 mock tool server (端口 4001)
├── realistic_attack_v2/v3/v4/v5/v6.py  # 6 个版本 case 生成器
├── mock_proxy_server.py          # mock proxy 链路验证
└── ...

src/system/proxy/
├── gov_proxy.py                  # 防护代理
├── audit_log.jsonl               # 测试审计日志(不入库)
└── __init__.py

data/system-design/e2e-pilot/
├── proxy_test_66.jsonl           # 66 条 BYPASSED 测试集
├── e2e_cases_v[3-6]_realistic_*.jsonl  # 各版本 case
├── e2e_results_*.jsonl           # 跑结果
├── README.md                     # 库概览
├── TEST-METHOD.md                # 详细测试方法
└── PROXY-RESULTS.md              # Proxy 拦截测试结果

docs/system/e2e-pilot/
├── RUN-PROXY-MODE.md             # 启动 proxy 模式指南
├── case-redesign-framework.md    # case 改造框架
├── plan-experiment.md            # 实验报告 v1
└── plan-experiment-v2.md         # 实验报告 v2
```

## 关键产出

- **66 条 BYPASSED 真实场景 case** — 覆盖 block-03 全部 50 个政务 skill
- **proxy 拦截率仅 13.6%** — 需要补强结构性规则(已在 docs/PROXY-RESULTS.md 列出)
- **deepseek-v4-flash 真实跑通** — 不是 mock 编造诱导,是真实业务话术诱导

## 答辩演示

直接展示 `data/system-design/e2e-pilot/e2e_results_v*_deepseek.jsonl`
中 BYPASSED 的 case,说明 LLM 真实被诱导调工具 (send_email/write/exec)。
