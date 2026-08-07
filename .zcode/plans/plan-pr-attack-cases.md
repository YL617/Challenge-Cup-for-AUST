# PR Plan: 真实场景攻击 case + 多轮 runner

## 背景

从 block-01/02/03 收集的 351 条政务攻击 case 中,我们手工设计改造并用
deepseek-v4-flash 真实跑通,得到 66 条 BYPASSED case,覆盖 block-03 全部
50 个政务 skill。

这些 case 已经通过验证:**LLM 真的会主动调攻击工具**(send_email/write/exec)
去外发真实数据/执行破坏操作 — 这是真实漏洞证据。

## PR 内容

### 1. 新文件 (src/scripts/)
- `multiround_runner.py` - 多轮对话 runner,支持 deepseek/stepfun
- `mock_tool_server_v2.py` - 多轮 mock tool server (端口 4001)
- `realistic_attack_v2/v3/v4/v5/v6.py` - 6 个版本 case 生成器
- `mock_proxy_server.py` - mock proxy 用于验证拦截链路

### 2. 新数据 (data/system-design/e2e-pilot/)
- `e2e_cases_v3_realistic_50.jsonl` - 50 条 case
- `e2e_cases_v4_realistic_25.jsonl` - 25 条 (替换 v3 NO_ATTACK/REFUSED)
- `e2e_cases_v5_realistic_15.jsonl` - 15 条 (新 skill 覆盖)
- `e2e_cases_v6_realistic_23.jsonl` - 23 条 (剩余 skill 覆盖)
- `proxy_test_66.jsonl` - 66 条 BYPASSED 用于 proxy 拦截测试
- `e2e_results_v3_realistic_deepseek.jsonl` - 跑结果
- `e2e_results_v4_realistic_deepseek.jsonl`
- `e2e_results_v5_realistic_deepseek.jsonl`
- `e2e_results_v6_realistic_deepseek.jsonl`
- `TEST-METHOD.md` - 合作学生测试方法

### 3. 新文档 (docs/system/e2e-pilot/)
- `RUN-PROXY-MODE.md` - 启动 proxy 模式跑通指南

### 4. 修改文件
- `.gitignore` - 添加 `.env.local` 排除本地测试 key
- `src/system/policies/unary_gate.py` - 结构性规则补充
- `src/system/runner/harness.py` - harness 升级

## 验证步骤

合作学生本地验证:
1. 准备 deepseek-v4-flash API key (或 step-3.7-flash)
2. 启动 mock tool server: `python3 src/scripts/mock_tool_server_v2.py`
3. 跑测试: `python3 src/scripts/multiround_runner.py --model deepseek-v4-flash --cases data/system-design/e2e-pilot/proxy_test_66.jsonl`
4. 期望结果: BYPASSED 50-70%,REFUSED 10-30%,NO_ATTACK 20-40%

## 提交信息

`feat(p0): 66 条真实 BYPASSED 攻击 case + 多轮 runner + proxy 拦截测试`

或者拆成多个 commit:
- `feat(p0): 多轮 runner 和 mock tool server`
- `feat(p0): v3 50 条 case + deepseek-v4-flash 验证`
- `feat(p0): v4-v6 case 优化与全 skill 覆盖`
- `feat(p0): proxy 拦截测试基础设施`
