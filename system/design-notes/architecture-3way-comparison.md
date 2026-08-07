# 防御框架架构对比图（自研 vs ArbiterOS vs XA_guard）

> 用在方案文档"相关工作对比"章节。  
> 三套系统都做"agent 工具调用安全治理"，但实现路径不同。

---

## 一、自研政务防护系统（src/system/）

```
                       [ OpenClaw Agent / 政务 SKILL 调用 ]
                                  │
                                  ▼ tool_call
   ┌──────────────────────────────────────────────────────────┐
   │                    指令解析层 (core/)                     │
   │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
   │  │ types.py │  │builder.py│  │tool_parsers│ │engine.py │  │
   │  │ SecurityType│ Instruction│ 政务工具解析 │ 管线驱动   │  │
   │  │ LEVEL_ORDER│ Taint 计算  │ read_image  │ 遍历策略   │  │
   │  │ compute_   │ 跨指令传播  │ kb_search   │ 累积 errors │  │
   │  │ prop_taint│ Spotlighting │ send_email  │             │  │
   │  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │
   └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────┐
   │                    策略引擎层 (policies/)                  │
   │                                                            │
   │  ┌──────────────────┐  ┌────────────────────────────┐  │
   │  │ UnaryGatePolicy    │  │ TaintPolicy               │  │
   │  │ - 5 声明式规则      │  │ - 默认启用                │  │
   │  │ - 谓词 DSL         │  │ - prop_trust >= conf     │  │
   │  │ - LLM Judge 注入   │  │ - input 工具 (read)      │  │
   │  │ - GOV-DENY-006     │  │ - output 工具 (write)   │  │
   │  └──────────────────┘  └────────────────────────────┘  │
   │  ┌────────────────────────────────────────────────────┐  │
   │  │ LLM Injection Judge (接 stepfun step-explore)      │  │
   │  │ - 0-10 打分，>= 6 = BLOCK                            │  │
   │  │ - 错峰注入 / OCR / base64 / 隐含软社工识别          │  │
   │  └────────────────────────────────────────────────────┘  │
   └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼ PolicyCheckResult
                          allow / log / approval / deny
                                  │
                                  ▼
                          [原 tool_call 执行 / 拦截]
```

**核心特点**：
- 单进程 Python（~2400 行），离线回放 80 case
- 真实接 LLM（stepfun step-explore 走 Anthropic Messages API）
- 缺：在线 OpenClaw 集成、AIBOM Skill 扫描、国密审计

---

## 二、ArbiterOS（cure-lab/ArbiterOS）

```
                       [ LiteLLM Proxy 拦截 LLM HTTP 响应 ]
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────┐
   │              Kernel 核心（policy 编排）                  │
   │  ┌──────────────┐  ┌────────────┐  ┌───────────────┐  │
   │  │ litellm_callback│ │policy_check│ │policy_runtime │  │
   │  │ (300KB 单文件) │ │ (456 行)   │ │  (54741 行!) │  │
   │  │ 拦截 LLM 响应 │ │遍历策略  │ │ 动态加载  │  │
   │  │ 提取 tool_call │ │ 累积结果  │ │ 配置热更新  │  │
   │  └──────────────┘  └────────────┘  └───────────────┘  │
   │                         │                              │
   │                         ▼                              │
   │   16 个 Policy（11 默认关，通过 policy_registry.json 启用）  │
   │   ┌─────────────────────────────────────────────────┐  │
   │   │ ✓ UnaryGatePolicy  ✓ TaintPolicy                 │  │
   │   │ ✓ RelationalPolicy ✓ SecurityLabelPolicy         │  │
   │   │ ✗ PathBudgetPolicy ✗ AllowDenyPolicy             │  │
   │   │ ✗ OutputBudget ✗ RateLimitPolicy                  │  │
   │   │ ✗ ResourceGuardPolicy ✗ DeletePolicy              │  │
   │   │ ✗ ExecComposite ✗ EfsmGatePolicy                   │  │
   │   │ ✗ AlignmentSentinel ✗ OpenClawPolicy               │  │
   │   └─────────────────────────────────────────────────┘  │
   │                                                            │
   │   LLM Judge (UG-060/061) 是 stub：                       │
   │   - 需手动配置 litellm_config.yaml 的 model/api_key/base │
   │   - 默认 0-5 分告警，无 LLM 实战                          │
   └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                          PolicyCheckResult
                                  │
                                  ▼
                          Litellm 适配回 OpenAI client
                          （实际生产部署才有这一步）
```

**核心特点**：
- 16 个 Policy 完整覆盖（含路径/频率/资源/输出预算）
- 通过 LiteLLM proxy 中间件接入，**生产可用**
- LiteLLM 配置复杂，需 3+ 配置文件
- 无 Spotlighting（直接看 path 关键词）
- 我们的 LLM Judge 实战上比它的 UG-060/061 stub 强

---

## 三、XA_guard（竞品 chuali-zi）

```
                       [ Trae / Cursor / CodeBuddy MCP client ]
                                  │
                                  ▼ MCP protocol (stdio / Streamable HTTP)
   ┌──────────────────────────────────────────────────────────┐
   │                  Agent Gateway (预检)                      │
   │   员工/Agent/数据域治理 + 成本归属 (governance.py, 756 行)│
   └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────┐
   │              6 道闸 pipeline (gates/, 1376 行)             │
   │                                                            │
   │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  │
   │  │ Gate1     │  │ Gate2     │  │ Gate3     │  │ Gate4     │  │
   │  │ input     │  │ plan      │  │ policy    │  │ taint     │  │
   │  │ (D1)      │  │ (D2)      │  │ (D2)      │  │ (D2)      │  │
   │  │ 339 行    │  │ 141 行    │  │ 170 行    │  │ 267 行    │  │
   │  │ RuleDet + │  │ 审批工作流│  │ OPAL/Rego │  │ taint 跟踪 │  │
   │  │ ModelDet  │  │           │  │ 双层策略  │  │           │  │
   │  └──────────┘  └──────────┘  └──────────┘  └──────────┘  │
   │                                                            │
   │  ┌──────────┐  ┌──────────┐                              │
   │  │ Gate5     │  │ Gate6     │                              │
   │  │ sandbox   │  │ audit     │                              │
   │  │ (D2)      │  │ (D4)      │                              │
   │  │ 117 行    │  │ 263 行    │                              │
   │  │ 沙箱隔离  │  │ 国密签名  │                              │
   │  └──────────┘  └──────────┘                              │
   │                                                            │
   │   + 预处理 Spotlighting (103 行) — non-user 来源 wrap   │
   └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                          [filesystem / shell / demo targets]
   ┌──────────────────────────────────────────────────────────┐
   │   AIBOM 准入网关 (aibom/, 3099 行) — D3 方向完整         │
   │   - scanner.py (AST)  - signing.py (签名)               │
   │   - schema_validator (CycloneDX 1.6)  - rater.py (评分) │
   │   - drift_monitor / external_generator (离线 L3 原型)    │
   └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                          政策数据库（/configs/）
                          国标 GB/T 45654-2025 映射
```

**核心特点**：
- 完整 MCP 双面代理（生产可用）
- 6 道闸 + 治理预检（最完整）
- AIBOM 3099 LOC（最重，但 D3 完整）
- CSAB-Gov 290 seed + dual 500 候选（评测最完整）
- 国密签名 + TSA（生产级审计）
- LLM Judge 是 stub（同 ArbiterOS）
- Spotlighting 是真做了的（Microsoft 论文）

---

## 四、三套系统横向对比

| 维度 | 自研（Phase 2.5.1）| ArbiterOS | XA_guard |
|------|----------------|----------|----------|
| **核心引擎** | core/builder + engine | policy_check + policy_runtime | 6 gates pipeline |
| **代码量** | ~2400 行 | ~8000 行（核心）| ~7300 行 |
| **集成方式** | 离线回放 + LLM Judge | LiteLLM Proxy 中间接入 | MCP 双面代理（生产部署）|
| **D1 攻击识别** | ✓ LLM Judge（真接 stepfun）+ 50 marker + Spotlighting | ✓ 16 Policy（11 关）| ✓ 6 Detector 融合 + Spotlighting |
| **D2 工具调用** | ✓ 3 关 + GOV-DENY-006 | ✓ 16 Policy | ✓ Gate2/3/4/5 完整 |
| **D3 供应链** | ✗ 缺 | ✗ 缺 | ✓✓ AIBOM 3099 LOC（CycloneDX 1.6）|
| **D4 评测审计** | △ JSONL 日志 | ✓ TS+签名 | ✓✓ 国密+TSA+Merkle |
| **LLM Judge** | ✓ 真接 stepfun 跑通 | ✗ stub（需手动配）| ✗ stub |
| **80 case recall** | **70.6%** | 10.3% | ~60-80%（估）|
| **80 case precision** | **98.0%** | 100% | ~90-95%（估）|
| **Spotlighting** | ✓ 简化（5 个工具名）| ✗ | ✓ 完整（103 行）|
| **国标映射** | ✗ 缺 | ✗ 缺 | ✓✓ CSAB-Gov 含 GBT-45654 |
| **评测 benchmark** | ✗ 缺 | ✗ 缺 | ✓ 290 seed + dual 500 |
| **提交包** | ✗ 待写 | - | ✓ 14 页 PDF+视频脚本 |

---

## 五、核心差异

**我们的优势**（在比赛"创新 25%"能拿分）：
- **LLM Judge 真接 stepfun 跑通**（ArbiterOS 和 XA_guard 都是 stub）
- **Spotlighting 简化版**（5 个工具名，比 XA_guard 完整版 100 行精简，但够用）
- **代码量 1/3**（同等效果下最精简）

**我们的弱项**（比赛"完整 20% + 应用 20%"扣分）：
- **D3 供应链无 AIBOM**（XA_guard 重头戏）
- **D4 审计无国密签名**（XA_guard 已完整）
- **无 XA-Bench 类 7 维度评测**（只有 1 个 recall 指标）
- **无国标映射**（XA_guard CSAB-Gov 引用 GB/T 45654-2025）

**后续 W2-W4 任务**（按比赛"完整/应用"补）：
- W2：AIBOM Skill 扫描器（补 D3 弱项）
- W3：XA-Bench 框架（补 D4 评测弱项）
- W4：国密签名 + GB/T 45654-2025 映射（补应用弱项）

---

## 六、放进方案文档的简化版（≤ 30 页）

### 第 4 章"相关工作对比"用这张：

| 维度 | 我们的系统 | ArbiterOS | XA_guard |
|------|----------|----------|----------|
| LLM Judge 实战 | ✓ 跑通 | ✗ stub | ✗ stub |
| Spotlighting | ✓ 简化 | ✗ | ✓ 完整 |
| AIBOM (D3) | ✗ | ✗ | ✓ 完整 |
| 国密审计 (D4) | ✗ | ✗ | ✓ 完整 |
| 国标映射 | ✗ | ✗ | ✓✓ |
| 代码量 | 2400 行 | 8000 行 | 7300 行 |
| 80 case F1 | 78.4% | 18.8% | ~80% |

**用一句话总结我们的定位**：
> 自研系统是"轻量+LLM实战"路线（2400 行、78% F1），与 ArbiterOS（8000 行、生产级 LiteLLM 适配）形成差异化，与 XA_guard（7300 行、MCP 双面代理+国密审计）形成"轻 vs 重"互补。

---

**维护者**: ZCode Agent
**审核**: @YangYu-NUAA
**用途**: 方案文档第 4 章"相关工作"
