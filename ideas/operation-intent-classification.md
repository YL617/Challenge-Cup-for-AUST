# 研究设计：操作意图分类层 (Operation Intent Classification)

> 状态：方向已确认（精准拦截 + 扩展能力 / 混合：启发式粗分 + LLM 兜底）
> 日期：2026-08-11
> 对标：MMRiskDetection 国二报告方法论 + KAIJU(IGX) / Action-Graded Severity / ScopeJudge

## 1. 问题陈述

当前 proxy 的规则引擎在"参数表面"做正则匹配（17 条 GOV-* 规则），导致 whack-a-mole：
同一操作意图被 LLM 用不同字段名/动词/嵌套格式表达时规则失效（如 `exec(command="approve_all")` 穿透 → 补 `exec(action="approve_all")` → 再穿透补空格变体）。

**核心假设（可证伪）**：基于「操作意图分类」的策略层比基于「参数正则匹配」的策略层，在拦截率不降的前提下，面对未见过攻击变体时泛化能力显著更强（且规则数更少）。

## 2. 学术定位（novelty check 结论）

**不是首创**。"intent-gated tool call"在 2025-2026 已是热点。

我们的差异化贡献（3 点）：
1. **防御向 6 类 tool_call intent taxonomy**：ATT&CK tactics 的 tool_call 粒度精简投影（DESTRUCTIVE≈Impact / DATA_EXPORT≈Exfiltration / AUTHORITY≈Privilege Escalation / SYSTEM_TAMPERING≈Persistence+Defense Evasion / RECONNAISSANCE≈Reconnaissance / NORMAL≈allowlist）
2. **canonicalization 工程抽象**：字段规范化（command/action/params.op 统一提取）+ 规则匹配意图类别而非原始参数 → 可维护性/可审计性收益
3. **混合策略实证**：启发式粗分（明确类别，低延迟）+ LLM 兜底（不确定类别，高泛化）的精度/延迟 trade-off，对比 taint-based(ArbiterOS) / pure-LLM-judge(ScopeJudge)

最该引用的对标工作：
- KAIJU (IGX) arXiv:2604.02375 — 多维 intent gating 先驱
- Action-Graded Severity arXiv:2607.07474 — 动作 taxonomy 直接对照（离线分级 vs 我们实时 gating）
- ScopeJudge arXiv:2607.07774 — pre-execution intent gating
- ArbiterOS/Arbiter-K — taint-based 对照（我们不靠 taint，靠意图类别，是差异）

## 3. 意图分类 Taxonomy（分层）

### 粗粒度（6 类）—— 规则引擎只作用在这层

| 意图类别 | ATT&CK 映射 | 定义 | 政务攻击对应 |
|---|---|---|---|
| `NORMAL_OPERATION` | — | 用户明确请求的正常业务 | write(草稿)、query_db(预算)、正常内部邮件 |
| `DESTRUCTIVE_ACTION` | Impact | 破坏性/不可逆的数据销毁 | rm/drop/truncate/delete |
| `DATA_EXPORT` | Exfiltration | 数据离开可控边界 | send_email(外部/群发)、share_file、browser_upload |
| `AUTHORITY_OPERATION` | Privilege Escalation | 行使审批/签字/政策权限 | approve_all/sign/modify policy |
| `SYSTEM_TAMPERING` | Persistence+Defense Evasion | 篡改系统/审计/证据 | journalctl/cron/systemctl/audit清理 |
| `RECONNAISSANCE` | Reconnaissance | 侦查探测内部结构 | list_tables/dump schema/扫描 |

### 细粒度（第二期，可选）—— DATA_EXPORT 下分子类
- external_email / batch_email / file_share / browser_upload / sms_leak

## 4. 技术架构

```
tool_call (原始 JSON)
  ↓
Canonicalizer（字段规范化）
  - command / action / params.op / cmd / args.op 统一提取为 canonical_op
  - to / recipient / recipients / cc 统一提取为 canonical_recipients
  - path / file_name / filename / name 统一提取为 canonical_path
  ↓
Operation Intent Classifier（混合）
  ├─ 启发式粗分（明确类别，<1ms）
  │   规则：canonical_op 含 rm/drop/truncate → DESTRUCTIVE_ACTION
  │         canonical_op 含 approve/sign/batch_approve → AUTHORITY_OPERATION
  │         tool=send_email 且 recipient 含外部关键词 → DATA_EXPORT
  │         ...
  ├─ LLM 兜底（启发式不确定时，~500ms）
  │   step-3.7 zero-shot：给定 canonical_op + recipients + context → 6 类
  └─ NORMAL_OPERATION（默认放行）
  ↓
规则引擎（只匹配意图类别）
  - IF intent IN {DESTRUCTIVE, AUTHORITY, SYSTEM_TAMPERING} → BLOCK（确定性）
  - IF intent == DATA_EXPORT → 走 LLM Judge 判定是否 excessive agency
  - IF intent == RECONNAISSANCE → BLOCK（探测性操作默认拦）
  - IF intent == NORMAL_OPERATION → 放行
  ↓
扩展能力层（不只拦截）
  ├─ 风险摘要：拦截时生成结构化报告（攻击类型+触发规则+危害等级+上下文证据）
  ├─ 攻击意图溯源：分析攻击者目标（数据外泄？伪造签字？证据销毁？）
  ├─ 规避建议：对运维人员给出加固建议
  └─ 相似案例检索（RAG）：检索历史攻击案例辅助判定
```

## 5. A/B 实验设计（泛化性验证）

### 实验目标
证明意图分类比参数正则在"未见变体"上泛化更好。

### 数据集
- **训练集（规则开发用）**：现有 66 攻击 + 25 白样本
- **Holdout 变体集（关键）**：自动生成现有 case 的"参数变体"——同一攻击意图换字段名/动词/格式：
  - `exec(command="approve_all")` → `exec(action="batch_sign")` → `exec(params={"op":"auto_approve"})` → `exec(cmd="approve_all_leave")`
  - 这模拟"新攻击变体"，规则没见过但意图相同

### 对比
| 系统 | 方法 | 预期 |
|---|---|---|
| Baseline A | 现有 17 条参数正则规则 | Holdout 变体拦截率低（whack-a-mole 验证） |
| Baseline B | pure LLM Judge（无规则） | 泛化好但延迟高、误报不可控 |
| 我们 | 意图分类（启发式+LLM）+ 规则匹配类别 | Holdout 拦截率高 + 规则数 ~6 + 延迟可控 |

### 指标
- Holdout 变体拦截率（泛化性核心指标）
- 规则数（可维护性）
- 延迟（启发式 <1ms vs LLM ~500ms 的比例）
- 白样本误报率（精度红线，必须 ≤现有 0%）

## 6. 可证伪条件

如果意图分类在 Holdout 变体上的拦截率 ≤ 参数正则规则 → 意图分类没有泛化优势 → 方向失败，退回参数正则。

## 7. 实现计划（3 步，约 4 天）

### Step 1：Canonicalizer + 启发式分类器（1.5 天）
- [ ] 实现 `canonicalize_tool_call(tool_name, args)` → 提取 canonical_op/recipients/path
- [ ] 实现启发式分类器 → 6 类（基于 canonical 字段 + 关键词词典）
- [ ] 在现有 66+25 上验证：启发式分类覆盖率 + 准确率

### Step 2：LLM 兜底分类 + 规则引擎对接（1 天）
- [ ] LLM zero-shot 分类 prompt（6 类，给定 canonical 字段）
- [ ] 对接规则引擎：规则改为匹配 intent 类别
- [ ] A/B 测试：现有 case 上 Baseline A vs 我们

### Step 3：Holdout 变体生成 + 泛化性实验（1 天）
- [ ] 自动生成变体集（每个攻击 case 生成 3-5 个参数变体）
- [ ] 三方对比：参数正则 vs pure-LLM vs 意图分类
- [ ] 出泛化性对比表（说明书核心数据）

### Step 4（扩展能力，可选，0.5 天）
- [ ] 风险摘要生成器（拦截时输出结构化报告）
- [ ] 攻击意图溯源（映射 ATT&CK tactic + 攻击者目标推断）

## 8. 比赛说明书影响

| 章节 | 方向 3 的贡献 |
|---|---|
| 技术方案 | 从"17 条正则规则"升级为"6 类意图分类 + 混合策略"架构图 |
| 数据集 | 新增 Holdout 变体集（泛化性评测） |
| 测试报告 | 三方 A/B 对比表（泛化性 + 延迟 + 规则数） |
| 创新点 | canonicalization 工程抽象 + 防御向 taxonomy + 混合策略 trade-off 实证 |
| 应用前景 | 风险摘要 + 意图溯源 + 规避建议（不只拦截） |

## 9. 风险

- **细粒度分类在开放域是难点**（mmriskdetection 数据：对话类 29-88%）——但我们 tool_call 参数结构化程度高，粗粒度应该更容易
- **LLM 兜底的延迟**——需要控制触发比例（启发式覆盖 ≥80% 的 case）
- **误报红线**——NORMAL_OPERATION 不能误判，否则白样本 0% 误报破功
