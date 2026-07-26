# 50 天冲刺计划（截至 2026-09-15 提交）

> 基于：
> - 比赛方案 PDF（XA-202620，4 大方向）
> - 竞品 XA_guard 调研（双面 MCP 代理，6 道闸，已 290 seed case）
> - 当前仓库现状（80+51+53 case、50 SKILL、Phase 2.4 LLM judge recall 62.1%）

---

## 一、比赛要求（再确认）

| 项 | 数据 |
|----|------|
| 题目 | XA-202620 面向政企场景的大模型智能体安全关键技术研究 |
| 发榜 | 中国雄安集团数字城市科技（国企） |
| 提交截止 | **2026-09-15 24:00**（约 50 天） |
| 终审 | 2026-11 |
| 提交物 | ① 技术方案 PDF（≤30 页）② 原型系统/代码 ③ 演示视频（≤10min）④ 报名表 |
| 评审 | 创新 25% + 效果 30% + 完整 20% + 应用 20% + 展示 5% |
| 奖项 | 擂主 10w、特等 1w×5、一等 0.5w×5、二等 0.2w×5、三等 0.1w×5 |

**最关键**：效果 30% 看 **量化指标**（攻击识别准确率、误报漏报、阻断效果、审计完整度）。

---

## 二、4 大研究方向 + 我们的对应

| 方向 | 比赛要求 | 我们现状 | 差距 | 优先级 |
|------|---------|---------|------|-------|
| **D1 攻击识别** | prompt injection/越狱/数据投毒自动识别 + 关联分析 | Phase 2.1 语义注入检测 + Phase 2.4 LLM judge (recall 62.1%, FP 0) | 缺：更多攻击类型（视觉注入、函数调用注入、跨会话投毒）+ 量化指标报告 | P0 |
| **D2 工具调用安全** | 文件/命令/浏览器/接口 细粒度权限 + 动态校验 + 审批 + 异常链阻断 | 5 条 unary 规则（exec/rm/curl/policy）+ taint policy + LLM judge | 缺：动态策略引擎（OPAL）、可插拔审批工作流、runtime 异常链检测 | P0 |
| **D3 插件/Skill 供应链** | 第三方组件代码行为检测 + 依赖分析 + 恶意逻辑识别 + 安全评级 | 50 SKILL（gov-*），但**没有 AIBOM 扫描** | 缺：Skill manifest 标准化、依赖图、风险评分、Skill lifecycle 管理 | P1 |
| **D4 评测 + 审计** | 多维度评测 + 风险指标 + 攻击复现 + 审计证据 | 80 case + 247 batch run results + JSONL 审计日志 | 缺：标准化评测 benchmark（XA-Bench）、可视化审计报告、attack replay 工具 | P0 |

**策略**：D2（我们的核心优势）+ D1（已有 Phase 2.4 算法）+ D3 增量做 + D4 包装。

---

## 三、竞品 XA_guard 对照

| 维度 | XA_guard | 我们 | 我们的差距 |
|------|---------|-----|----------|
| **6 道闸** | Gate1 攻击识别 / Gate2 人工审批 / Gate3 策略 DSL / Gate4 污点 / Gate5 沙箱 / Gate6 审计 | Gate1（语义+LLM）/ Gate2（部分/UnaryGate）/ Gate4 污点 | 缺 Gate3 完整 DSL、Gate5 沙箱 |
| **290 seed case** | + AgentDojo/InjecAgent smoke | 80 + 51 + 53 = 184 | 差 106 条，但更聚焦政务 |
| **AIBOM Gateway** | L3 原型 | 无 | **必须补**（D3 评分项） |
| **XA-Bench** | 评测 + 审计可视化 | 散在 _audit 报告 | 缺统一 benchmark |
| **提交包** | D1 PDF 完成（14 页）+ D2 远程仓库 + D4 报名 | 数据 + 代码有，**PDF + 视频没做** | **最关键** |
| **782 测试** | 781 通过 | 80 case harness | 少但够用 |
| **7300 LOC Python** | 双面代理 | ~2000 行核心 | 10× 差距 |

**我们的差异化优势**：
1. **政务 SKILL 库**（50 个 gov-*-assistant）——竞品没做
2. **真实案例 + 真实 batch run**（247 条）——竞品是合成 seed
3. **ArbiterOS 兼容 case 格式** ——便于迁移
4. **LLM judge 实战**（stepfun 跑通）——竞品无

**竞品的优势**：
1. **6 道闸完整**（我们只有 3 个）
2. **AIBOM 评估**（L3 原型）
3. **提交包完整**（PDF + 视频）
4. **工程化更扎实**（7300 LOC、782 测试）

---

## 四、50 天冲刺 WBS（按周）

### W1-W2（7/26-8/8）：**D1 强化 + D2 完善**（核心算法升级）

| 任务 | 责任 | 产出 |
|------|------|------|
| Phase 3 多模态 LLM judge（视觉注入/函数注入） | 维护者 | llm_injection_judge_v2 |
| 加视觉 prompt injection case 20 条 | 维护者 | data/block-01/expanded_v2 |
| TaintPolicy 改用 prop_trust（input 工具）已经做了 | — | — |
| 实现 Gate3 策略 DSL（OPAL 子集） | 维护者 | policies/policy_dsl.py |
| 写 ablation 报告（Phase 1 vs 2.1 vs 2.4） | 维护者 | docs/ablation.md |

**验收**：recall 从 62.1% 提到 70%+；策略 DSL 能加载 YAML

### W3-W4（8/9-8/22）：**D3 增量 + Skill AIBOM**

| 任务 | 责任 | 产出 |
|------|------|------|
| 50 SKILL 加 manifest 标准化（name/version/permissions/deps） | 3 号 | skills/*/MANIFEST.yaml |
| 写 AIBOM 扫描器（read SKILL.md 提风险评分） | 维护者 | src/system/d3/skill_aibom.py |
| 跑扫描生成 50 SKILL 风险报告 | 维护者 | data/_audit/aibom_report.md |
| 改 doc-deps + supply-chain 简评 | 维护者 | docs/SKILL-CHAIN.md |

**验收**：50 SKILL 全部有 manifest + 风险评分

### W5（8/23-8/29）：**D4 评测 + 审计报告**

| 任务 | 责任 | 产出 |
|------|------|------|
| 写 XA-Bench（综合 benchmark，~30 测例） | 维护者 + 学生 | bench/xa_bench/ |
| 跑 247 case + 100 新 case → 出 D4 报告 | 维护者 | docs/D4-eval-audit.md |
| 写 attack-replay 工具（XML case 重新跑） | 维护者 | src/system/d4/replay.py |
| 整理审计 JSONL → 报告 + 图表 | 维护者 | docs/audit-summary.html |

**验收**：XA-Bench 能跑，结果可复现

### W6（8/30-9/5）：**Phase 4 OpenClaw 端到端**

| 任务 | 责任 | 产出 |
|------|------|------|
| 真实 OpenClaw agent 调 gov SKILL + 我们的防护系统 | 维护者 | phase-4 demo video script |
| 录 3 个端到端 demo（正常起草/有攻击/被拦） | 维护者 | demo/*.mp4 |
| 验证 50 SKILL 实际工作 | 维护者 | test/integration/ |

**验收**：真 OpenClaw + 真 LLM（stepfun）+ 真防护系统跑通

### W7-W8（9/6-9/15）：**方案文档 + 提交包**（最关键 9 天）

| 任务 | 责任 | 产出 |
|------|------|------|
| **写技术方案 PDF**（≤30 页） | 维护者 + 4 号 | output/pdf/XA-202620-tech-report.pdf |
| 录演示视频（≤10min） | 维护者 | output/video/demo.mp4 |
| 准备报名材料 | 4 号 | 报名表、邮件草稿 |
| 9-15 前发送邮件到 caoruyue@chinaxiongan.com.cn | 4 号 | 邮件截图 |

**验收**：邮件发出、收到确认

---

## 五、风险与应对

| 风险 | 应对 |
|------|------|
| LLM judge 延迟（8 分钟/80 case）→ 答辩演示卡 | 加并发 + 缓存（phase 4 顺带做） |
| 50 天时间紧 | 优先 D1+D2（优势），D3+D4 增量做 |
| 评委问"为什么不用 ArbiterOS" | 答"ArbiterOS 通用，我们的政务场景化 + LLM judge 创新点" |
| 竞品 XA_guard 已在 AIBOM 做了 | 我们的 SKILL 库 + 真实 batch run 数据是差异化 |
| 方案 30 页写不完 | 严格按评分维度：效果 30%（数据图表）> 创新 25%（LLM judge + 语义注入）> 应用 20%（政务场景）> 完整 20%（架构图）> 展示 5% |

---

## 六、和我们现有资源对位

| 资源 | 现有 | 用途 |
|------|------|------|
| 80+51+53 case | ✅ | D1 攻击识别核心数据 |
| Phase 2.4 LLM judge（62.1% recall） | ✅ | **D1 的核心创新点**（论文/答辩都用） |
| 50 SKILL（gov-*-assistant） | ✅ | D2 工具调用 + D3 组件生态 |
| 247 batch run + summary + 失败分析 | ✅ | D4 审计证据 |
| OpenClaw 真实部署 | ✅ | D2 端到端演示 |
| stepfun step-explore key | ✅ | LLM judge 实测 |
| 6 个 design-notes 设计文档 | ✅ | 方案文档素材 |
| 自研防护系统 ~2000 行 | ✅ | 答辩 demo |

**缺**：
- ❌ 方案 PDF 文档
- ❌ 演示视频
- ❌ XA-Bench 统一 benchmark
- ❌ AIBOM Skill 扫描
- ❌ 100 条新 case 扩充
- ❌ OPAL 策略 DSL
- ❌ 商业可行性分析

---

## 七、下一步行动（先做 3 件事）

1. **W1 第一周任务**（本周）：
   - 选 10 条新 case（视觉注入、函数调用注入、跨会话投毒）补 Phase 2.5
   - 写 ablation 报告
2. **5 个学生分工**：
   - 4 号：风险分级 + 商业可行性 + 报名材料
   - 其他：50 SKILL 加 manifest
3. **每周五 16:00 同步会**（半小时）：看进度、调计划

**最关键 deadline**：**9-15 24:00 邮件发出**，倒推所有任务必须在 9-14 23:59 完成。

---

**负责人**：指挥老师（你）
**执行**：维护者（我）+ 学生团队
**检查点**：每周五 16:00
