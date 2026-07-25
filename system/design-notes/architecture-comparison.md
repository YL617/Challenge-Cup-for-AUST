# 防护框架架构对比：自研系统 vs ArbiterOS

> 对比维度：从 case JSON 进入到判定输出的完整数据流。
> 重点：**我们和 ArbiterOS 怎么实现同一件事，差异在哪**。

---

## 一、整体数据流对比

```
                      ArbiterOS                                 自研系统（src/system/）
┌──────────────────────────┐                ┌──────────────────────────┐
│  Case JSON (prior+current)│                │  Case JSON (prior+current)│
└──────────┬───────────────┘                └──────────┬───────────────┘
           ▼                                            ▼
┌──────────────────────────┐                ┌──────────────────────────┐
│  litellm_callback.py      │                │  runner/harness.py        │
│  - 拦截 LLM 响应          │                │  - 离线回放（无 LLM）     │
│  - 构造 tool_call        │                │  - 提取 tool_call         │
│  (300KB 单文件)          │                │  - 调策略引擎            │
└──────────┬───────────────┘                │  (374 行)                │
           ▼                                  └──────────┬───────────────┘
┌──────────────────────────┐                          ▼
│  instruction_parsing/     │                ┌──────────────────────────┐
│  - builder.py            │                │  core/builder.py          │
│  - tool_parsers/         │  ──对位──>     │  - 加 step_trust 传播     │
│    (DSL YAML 引擎)       │                │  - 语义注入标记传播        │
│  - 8 个 tool parser 目录  │                │  (198 行)                 │
│  (~2500 行)              │                └──────────┬───────────────┘
└──────────┬───────────────┘                          ▼
┌──────────────────────────┐                ┌──────────────────────────┐
│  policy_check.py          │                │  core/engine.py            │
│  - 遍历 policy_registry   │                │  - 遍历 [UnaryGate,        │
│  - 聚合 PolicyCheckResult│  ──对位──>     │    TaintPolicy]            │
│  (456 行)                │                │  - 聚合                  │
└──────────┬───────────────┘                │  (68 行)                  │
           ▼                                  └──────────┬───────────────┘
┌──────────────────────────┐                          ▼
│  policies/                │                ┌──────────────────────────┐
│  - unary_gate_policy.py  │  ──对位──>     │  policies/unary_gate.py   │
│    (2366 行，含 LLM judge)│                │  - 谓词 DSL（25 操作符）   │
│  - taint_policy.py       │                │  - LLM Judge（接 stepfun）│
│  - relational_policy.py  │                │  - 5 条默认规则            │
│  - security_label_policy │                │  (616 行)                 │
│  - 11+ 个其他 policy     │                │  policies/taint_policy.py  │
│  (总计 ~8000 行)         │                │  (168 行)                 │
└──────────┬───────────────┘                └──────────┬───────────────┘
           ▼                                            ▼
┌──────────────────────────┐                ┌──────────────────────────┐
│  tool_parsers/engines/    │                │  llm_injection_judge.py   │
│  - engine.py (10853 bytes)│  ── 替代 ──>  │  - stepfun step-explore  │
│  - YAML DSL              │                │  - 0-10 打分             │
│  - run_cases.py (批跑)    │                │  (200 行)                │
│  - 8 个平台适配器         │                │  - 未做：run_cases/适配    │
└──────────┬───────────────┘                └──────────┬───────────────┘
           ▼                                            ▼
┌──────────────────────────┐                ┌──────────────────────────┐
│  PolicyCheckResult        │                │  PolicyCheckResult        │
│  - modified              │                │  - modified              │
│  - response              │                │  - response              │
│  - error_type            │                │  - error_type            │
│  - policy_names          │                │  - policy_names          │
│  - inactivate_error_type │                │  (更精简)                 │
│  - local_confirmation_*  │                │                          │
└──────────┬───────────────┘                └──────────┬───────────────┘
           ▼                                            ▼
┌──────────────────────────┐                ┌──────────────────────────┐
│  Litellm 适配 OpenAI     │                │  （无 OpenClaw 适配）     │
│  HTTP 协议分发回 agent   │                │  只做离线回放             │
└──────────────────────────┘                └──────────────────────────┘
```

---

## 二、模块级对比

| 维度 | ArbiterOS | 自研系统 | 差异原因 |
|------|----------|---------|---------|
| **总代码量** | ~8000 行（核心策略） | ~2000 行（含 50 个 SKILL 业务逻辑） | 砍了不需要的功能（langfuse 可视化、codex cli、anthropic-cli 等） |
| **策略数** | 16 个 Policy（11+ 默认关） | 3 个有效（UnaryGate + TaintPolicy + LLM Judge） | 专注政务场景 |
| **LLM judge** | UG-060/UG-061（需本地 litellm_config.yaml） | llm_injection_judge.py（接 stepfun API） | 我们的复用 OpenClaw 已有 profile |
| **注入检测** | 无（靠 TaintPolicy 上下文跟踪） | ✅ Phase 2.1 语义注入检测层 | 业务驱动（我们漏检 6 条失败 case） |
| **敏感路径** | 无内置（靠 taint 抽象） | ✅ sensitive_paths.yaml | 政务场景硬需求 |
| **平台适配** | 4 个（OpenClaw/Hermes/Nanobot/Codex） | 0（只离线回放） | 答辩演示前才需要 |
| **可观测性** | Langfuse 完整集成 | 0 | 不在范围内 |
| **OAS schema** | 全套（types/judges/audits） | 简化版（Dict + Enum） | 牺牲扩展性换简洁 |

---

## 三、关键设计决策差异

### 3.1 工具调用识别

| 项 | ArbiterOS | 我们 |
|----|---------|----|
| 数据源 | LiteLLM 拦截 LLM HTTP response | 离线 case JSON 读取 |
| 协议 | OpenAI-compatible HTTP | 直接读 `current.tool_calls` 字段 |
| 实时性 | 真实 agent 运行拦截 | 测试用，事后回放 |
| 适配 | LiteLLM proxy 中间件 | 无（直接读 JSON） |

### 3.2 策略引擎

| 项 | ArbiterOS | 我们 |
|----|---------|----|
| 调度 | `policy_registry.json` 动态加载 + `policy_engine_bat.py` 备份 | 显式 `policy_classes=[UnaryGate, TaintPolicy]` 列表 |
| 策略接口 | `Policy.check(instructions, current_response, latest_instructions, trace_id, **kwargs)` | 相同 |
| 执行顺序 | 按注册顺序 | 按列表顺序 |
| 失败回退 | `apply_policy_enforcement_mode` observe-only 模式 | 无（直接 fail-close） |

### 3.3 谓词 DSL

| 项 | ArbiterOS | 我们 |
|----|---------|----|
| 操作符 | ~25 个（all/any/not/eq/ne/gt/ge/lt/le/between/in/not_in/contains/intersects/contains_all/subset_of/starts_with/ends_with/matches/truthy/falsy/exists/missing/runtime_allow_deny + var/const/len/count_intersections） | **完全复用** ArbiterOS 的设计 |
| 解析方式 | 闭包 + accumulator（_make_parser） | **直接复用** 同样的 _eval_predicate |
| 自创规则 | YAML DSL 引擎（~10KB） | 5 条规则 + JSON 格式 |
| 复杂度 | 支持 PathPass/RegexPass/DefaultPass/NumericPass | 不需要这么复杂（政务场景规则少） |

### 3.4 语义注入检测（**自研独有**）

| 项 | ArbiterOS | 我们 |
|----|---------|----|
| 机制 | TaintPolicy 上下文跟踪 | **新加 `_adjust_propagated_trust` 层** |
| 检测内容 | 仅 trust 等级 | **正则匹配** `[隐藏指令]` `[伪造身份]` `ignore` `disregard` 等 25 个中英文 marker |
| 传播机制 | reference_tool_id 链 | **跨 instruction 扫描**：trace 里任何 prior 标了 injection，当前 instruction 强制 LOW |
| LLM judge | UG-060/UG-061 需手动配 litellm | **自动接 stepfun API**，0-10 打分 |

### 3.5 Taint 判定

| 项 | ArbiterOS | 我们 |
|----|---------|----|
| 工具分类 | `_DEFAULT_INPUT_TOOLS` / `_DEFAULT_OUTPUT_TOOLS` frozenset | **相同**（复用） |
| input 工具判定 | `trust >= conf` | **相同** + input 改用 `prop_trust`（修复原 bug） |
| output 工具判定 | `trust >= prop_conf` | **相同**（不改，因为会误拦 6 个 safe case） |
| 默认开关 | **默认关闭** | **默认开启**（用 prop_trust，自动被语义注入激活） |

---

## 四、性能对比（80 条 case）

| 指标 | ArbiterOS（严格） | 自研 Phase 1 | 自研 Phase 2.4 |
|------|-----------------|-------------|--------------|
| TP | 6 | 16 | **36** |
| FN | 52 | 42 | **22** |
| **Recall** | 10.3% | 27.6% | **62.1%** |
| FP | 0 | 0 | **0** |
| Precision | 100% | 100% | **100%** |
| 6 目标 case | 3 | 3 | **6（全拦）** |
| 跑 80 case | 几秒 | <1秒 | 8 分钟（LLM 调用） |

**62.1% 远超 ArbiterOS 10.3%**——但要承认：这是因为 ArbiterOS 在我们测试时**默认关闭了大部分 Policy + 没有 TaintPolicy 启用 + 没有 LLM judge**，跑的是"裸奔"状态。如果启全，ArbiterOS 应该能到 50-70%。

---

## 五、谁"更对"——其实各有适用场景

| 场景 | 选 ArbiterOS | 选自研 |
|------|------------|-------|
| 通用 agent 安全治理 | ✅ 完胜（多平台适配、Langfuse、production-grade） | ❌ 不支持 |
| 比赛答辩 + 政务场景快速出 demo | ❌ 太重，要装 litellm+langfuse | ✅ 800 行核心，跑 80 case 8 分钟 |
| 研究"LLM judge 实际效果" | ❌ 无（默认仅规则） | ✅ 集成 LLM 评分 |
| 长期生产部署 | ✅ | ❌（无平台适配、无可观测性） |

**结论**：自研系统 = 比赛 demo + 算法验证。ArbiterOS = 生产环境。两者互补，不冲突。

---

## 六、自研系统最该补的（优先排序）

1. **平台适配层**（Phase 4）：接入 OpenClaw 真实 agent，把 case JSON 从真实 tool_call 截获（不是离线读取）
2. **Langfuse 等可观测性**（可选）：让评委看到 trace 全链路
3. **run_cases 批跑脚本**（直接复用 ArbiterOS 的 `redteam/_automation/run_cases.py` 即可）
4. **多 provider LLM judge 缓存**：避免每次重复调 LLM
