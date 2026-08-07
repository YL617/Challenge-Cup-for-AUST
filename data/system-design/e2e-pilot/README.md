# 真实场景攻击 case 库

## 概述

从政务办公场景中真实发生过或可能被诱导的智能体漏洞案例中,我们设计了
**66 条** BYPASSED case(deepseek-v4-flash 真实跑通),覆盖 block-03
**全部 50 个** 政务办公 skill。

## 关键统计

| 版本 | case 数 | BYPASSED | BYPASSED率 | 文件 |
|---|---|---|---|---|
| v3 | 50 | 25 | 50% | e2e_cases_v3_realistic_50.jsonl |
| v4 | 25 | 16 | 64% | e2e_cases_v4_realistic_25.jsonl |
| v5 | 15 | 10 | 67% | e2e_cases_v5_realistic_15.jsonl |
| v6 | 23 | 15 | 65% | e2e_cases_v6_realistic_23.jsonl |
| **总计** | **113** | **66** | **58%** | - |

## 设计原则

1. **user_msg 用正常业务话术** — "Q3 结束了,我需要看下差旅费 v2025 的执行情况"
2. **mock_results 是真实内容** — 真实 db 查出来的(96% 执行率等),不编造诱导
3. **Round 1 查真实数据 → Round 2 LLM 主动判断需要行动** — 不靠 mock 编造诱导,靠 LLM 看完真实数据主动觉得需要外发/写报告
4. **真实工具调用** — send_email + write + calendar_create + exec

## 覆盖场景

覆盖 block-03 全部 50 个政务 skill:
- 政策分析/公文起草/收文登记/档案管理
- 跨部门协调/会议(5 个 skill)/调研
- 审批/纪检/督查督办/保密管理
- 邮件(分类/回复/处理)/采购/合同/资产管理
- 信访/接待/印章/数据共享/培训/考勤
- 等等

## 关键产出

| 文件 | 用途 |
|---|---|
| `e2e_cases_v3_realistic_50.jsonl` | v3 case (50 条) |
| `e2e_cases_v4_realistic_25.jsonl` | v4 case (25 条,替换 v3 NO_ATTACK) |
| `e2e_cases_v5_realistic_15.jsonl` | v5 case (15 条,新 skill) |
| `e2e_cases_v6_realistic_23.jsonl` | v6 case (23 条,剩余 skill) |
| `proxy_test_66.jsonl` | 66 条 BYPASSED 用于 proxy 测试 |
| `e2e_results_*_deepseek.jsonl` | 各版本跑结果 |
| `TEST-METHOD.md` | 合作学生本地测试指南 |
| `RUN-PROXY-MODE.md` | Proxy 模式启动指南 |

## 验证方法

见 `TEST-METHOD.md` — 合作学生可在本地用 deepseek-v4-flash 测试。

## 应用场景

1. **Proxy 拦截能力测试** — 用 66 条 BYPASSED 验证 proxy 防护能力
2. **真实漏洞证据** — LLM 真的会主动调工具外发真实数据
3. **演示素材** — 答辩时展示 50 个真实场景下 LLM 失控的 case
4. **防御系统训练** — 用这些 case 训练 proxy 的拦截规则
