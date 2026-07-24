# 比赛交付 Plan（基于远程 main 真实状态）

> 创建时间：2026-07-24
> 基于远程 main（commit 57c2dc7）的完整资产盘点 + 任务书验收口径对照。

---

## 一、比赛到底要交什么

任务书第十一节的核心验收口径：

> **每个人的工作必须能进入后续总框架。不能只交"资料阅读笔记"。必须交可转换、可运行、可评测、可展示的资产。**

### 1.1 五块数据资产（任务书明确要求）

| 块 | 验收口径 | 远程 main 状态 | 缺口 |
|----|---------|---------------|------|
| 1 号 | 整理表 + 改写案例 + 路径索引 | ✅ 80 条 + 247 扩充 | 无 |
| 2 号 | 筛选表 + 改写 + 排除说明 | ✅ 51 条 + 511 行筛选 | 无 |
| 3 号 | 5 Skill + 正常/攻击案例 | ✅ 5 skill + 53 条 | 无 |
| 4 号 | 四级矩阵 + 策略规则 + **案例映射** | ⚠️ 矩阵 + 12 规则有，**映射表空** | **case_to_policy_mapping 只有表头** |
| 5 号 | 批跑索引 + results/parsed/raw + 失败分析 | ✅ 80 条全归档 | 无 |

### 1.2 系统设计资产（答辩加分，任务书暗示）

任务书总目标提到"能接入后续 OpenClaw 办公智能体测试"，第十一节"总框架接入"暗示需要：
- 架构图 ✅
- 部署文档 ✅
- 系统设计文档 ✅
- **可演示的拦截效果** ← 这是我们要自研的

### 1.3 我们额外要做的（超越任务书的加分项）

| 加分项 | 价值 |
|--------|------|
| 自研政务防护系统（基于 ArbiterOS 精简） | 证明"不只是跑别人的工具，而是有自己的防护贡献" |
| 严格口径复跑（28/80 → 目标 ≥80%） | 证明自研系统补齐了 ArbiterOS 的缺口 |
| 答辩演示（拦截一条攻击 case） | 可视化效果 |

---

## 二、现有数据资产够不够

### 2.1 够的部分

| 资产 | 数量 | 位置 |
|------|------|------|
| 核心 case 库 | 80 条（block-01） | `data/block-01-.../gov_rewrite/arbiteros_cases_gov_rewrite.jsonl` |
| 扩充 case 库 | 247 条（80 核心 + 80 变体 + 87 新 OWASP） | `arbiteros_cases_gov_rewrite_expanded.jsonl` |
| 公开数据集改写 | 51 条（block-02） | `data/block-02-.../gov_cases/...` |
| 原创政务案例 | 53 条（block-03） | `data/block-03-.../cases/gov_original_cases.jsonl` |
| ArbiterOS 标准格式 case | 80 条（按 skill 分组） | `data/arbiteros_standard_cases/` |
| 批跑产物 | 80 条 runs + 80 个 run_outputs | `data/block-05-.../runs/` |
| 策略规则草案 | 12 条 GOV-* | `data/block-04-.../policy/gov_policy_rules.yaml` |
| 语义规则设计 | 4 条 GOV-SEM-* | `data/block-04-.../policy/gov_semantic_rules.yaml` |
| 风险矩阵 | 4 级 | `data/block-04-.../risk_level_matrix.xlsx` |
| 失败 case 分析 | 6 条根因 + 改进建议 | `system/design-notes/gap-analysis-...` |

### 2.2 不够的部分

| 缺口 | 影响 | 优先级 |
|------|------|--------|
| **block-04 `case_to_policy_mapping` 空** | 4 号验收不合格 | **P0** |
| block-04 策略规则未基于 case 归纳 | 任务书要求"从案例归纳"不是"提前拍脑袋" | P0 |
| 自研系统代码 | 加分项，但答辩没代码 = 只有文档 | P1 |
| 揭榜挂帅选题 PDF / 挑战杯说明 PDF | 比赛材料完整性 | P1 |
| 答辩演示材料（拦截效果可视化） | 答辩加分 | P2 |

---

## 三、没有 OpenClaw 怎么测试

这是最关键的技术问题。答案：**用 policy_test_harness 做离线回放测试，不需要运行时 LLM**。

### 3.1 测试原理

```
我们的 80 条 case（prior + current JSON）
    ↓
policy_test_harness（离线回放）
    ↓
InstructionBuilder 解析 → Instructions 列表
    ↓
我们的策略引擎（UnaryGate + Taint + Relational + Semantic）
    ↓
PolicyCheckResult（allow / deny / approval）
    ↓
对比 expected（safe=放行，unsafe=阻断）
    ↓
准确率 / 召回率 / F1
```

**关键**：case 里的 `prior/current` 是**预构造的 assistant 输出**，不需要真 LLM 生成。`policy_test_harness` 直接解析这些 JSON 做 policy 判定。

### 3.2 两条测试路径

| 路径 | 用途 | 需要 LLM | 需要 OpenClaw |
|------|------|---------|-------------|
| **路径 A：离线回放** | 用 80 条预构造 case 测策略准确率 | ❌ 不需要 | ❌ 不需要 |
| **路径 B：在线拦截** | 接 OpenClaw，真跑一个 gov skill，看拦截效果 | ✅ 需要 | ✅ 需要 |

**比赛阶段策略**：
- **现在**：只走路径 A（离线回放），用 80 条 case 验证策略效果
- **答辩前**：再走路径 B（装 OpenClaw + 接 LLM），做 1-2 个端到端演示

### 3.3 路径 A 的具体测试方案

```bash
# 1. 用我们的策略引擎跑 80 条 case
python -m src.system.runner.test_harness \
  --cases data/block-01-.../arbiteros_cases_gov_rewrite.jsonl \
  --config src/system/config/ \
  --output results/

# 2. 对比 expected（safe 应放行，unsafe 应阻断）
python -m src.system.runner.report \
  --results results/ \
  --baseline data/block-05-.../runs/20260712T025913.880037Z/summary.json
```

输出指标：
- Accuracy = (TP + TN) / 80
- Precision = TP / (TP + FP)
- Recall = TP / (TP + FN)  ← **这是核心：unsafe 案例被拦截的比例**
- F1

**目标**：Recall 从 ArbiterOS 的 10.34%（严格口径，6/58）提升到 ≥ 80%（≥ 46/58）。

### 3.4 路径 B 的最小可行演示（答辩用）

不需要装完整 OpenClaw，只需要：

1. 装 ArbiterOS Kernel（`uv sync`）
2. 配 1 个 LLM endpoint（任何 OpenAI-compatible API）
3. 写一个 mock agent：发一个 gov skill 的 tool call，经过 ArbiterOS
4. 演示：攻击 case 被拦截 + 正常 case 被放行

这个**不需要 OpenClaw 本体**，只需要一个能发 tool call 的最小 agent harness。

---

## 四、完整工作 Plan

### Phase 0：补 block-04 缺口（1 周，P0）

| 任务 | 负责人 | 产出 | 验收 |
|------|--------|------|------|
| 4 号基于 80+51+53=184 条 case 做风险归纳 | 4 号学生 | `case_to_policy_mapping.xlsx`（每条 case → 风险等级 + 规则） | ≥ 184 行数据 |
| 补困难安全案例（safe:unsafe = 1:5 → 1:2） | 4 号 + 维护者 | 42 条 hard negative case | safe 占比 ≥ 33% |
| 完善策略规则（从案例归纳，不是拍脑袋） | 4 号 | `gov_policy_rules.yaml` 更新 | 每条规则标注对应 case |

### Phase 1：自研系统核心引擎（2 周，P1）

| 任务 | 产出 | 验收 |
|------|------|------|
| 从 ArbiterOS 提取 InstructionBuilder + types | `src/system/core/trace.py` (~350 行) | 能解析 80 条 case |
| 提取 policy_check + Policy 基类 | `src/system/core/engine.py` (~150 行) | 能遍历多个 Policy |
| 精简 UnaryGatePolicy | `src/system/policies/unary_gate.py` (~500 行) | 跑通 80 条，复现 74/80 |
| 写 test_harness（兼容 ArbiterOS case 格式） | `src/system/runner/test_harness.py` (~300 行) | 能批量跑 + 生成 summary |

### Phase 2：补 6 条失败 case（2 周，P1）

| 任务 | 产出 | 验收 |
|------|------|------|
| 实现政务来源识别 | `src/system/policies/context_taint.py` (~200 行) | 拦截 DOC-006/MAIL-004/MAIL-006/CROSS-004 |
| 实现敏感路径分级 | `src/system/config/sensitive_paths.yaml` | budget/salary/hr/finance 自动标 confidential |
| 实现政务关系规则 | `src/system/policies/relational.py` (~400 行) | 拦截 OWASP-004/OWASP-005 |
| 默认开启 TaintPolicy | policy_registry 配置 | 6 条 case 全拦截 |
| **全量回归** | 80 条 case 100% 通过 | safe 不误报 + unsafe 全拦截 |

### Phase 3：答辩材料（1 周，P2）

| 任务 | 产出 |
|------|------|
| 生成揭榜挂帅选题 PDF + 挑战杯说明 PDF | `docs/topic/` + `docs/competition/` |
| 演示脚本（攻击拦截可视化） | `docs/DEMO_WALKTHROUGH.md` 更新 |
| 最终答辩报告 | `data/_audit/final_defense_summary.md` 更新 |
| 最小 agent harness（路径 B 演示） | `src/system/demo/` |

### Phase 4：在线演示（答辩前，P2）

| 任务 | 产出 |
|------|------|
| 装 ArbiterOS Kernel | 本地 `~/ArbiterOS` |
| 配 LLM endpoint | `.env` 填 API key |
| 写 mock agent 发 gov skill tool call | `src/system/demo/mock_agent.py` |
| 演示：1 条正常 case 放行 + 1 条攻击 case 拦截 | 录屏 / 现场 |

---

## 五、时间线

```
Week 1   Phase 0：补 block-04 + hard negative（4 号）
Week 2-3 Phase 1：核心引擎（维护者）
Week 4-5 Phase 2：补 6 条失败 case（维护者）
Week 6   Phase 3：答辩材料（全员）
答辩前   Phase 4：在线演示（维护者）
```

**Phase 0 和 Phase 1 可以并行**：4 号做风险分级的同时，维护者开始写核心引擎。

---

## 六、风险与决策点

| 风险 | 影响 | 缓解 |
|------|------|------|
| 4 号不配合 / 时间不够 | block-04 验收不合格 | 维护者兜底（基于 case 自动生成映射表草案） |
| 自研系统跑不出 ≥80% | 答辩加分项落空 | 至少能展示"35% → 提升了多少"，过程本身有价值 |
| 网络不稳定影响 clone/push | 协作效率低 | 用 API 操作 GitHub，减少 git push 依赖 |
| 揭榜挂帅赛截止时间不确定 | 整体节奏失控 | 需指挥老师确认提交截止时间 |

### 需要指挥老师确认

1. **揭榜挂帅提交截止时间**是什么时候？（决定 Phase 2 能不能做完）
2. **4 号学生是否在线 / 能否参与**？（决定 block-04 谁来做）
3. **答辩是线上还是线下**？（决定 Phase 4 演示方式）

---

**维护者**：指挥老师 + ZCode Agent
**状态**：待指挥老师确认后按 Phase 0 → 1 → 2 → 3 → 4 执行