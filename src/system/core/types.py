"""
core/types.py — 核心数据结构（从 ArbiterOS instruction_parsing/types.py 提取精简）

保留：SecurityType / Instruction / LEVEL_ORDER / compute_prop_taint_for_instruction
去掉：CONCRETE_LEVELS / ALL_LEVELS / collect_levels（仅测试用）
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Literal, NamedTuple, Optional, cast

# ---------------------------------------------------------------------------
# 类型别名
# ---------------------------------------------------------------------------

SecurityType = Dict[str, Any]
"""安全属性字典，由 make_security_type 构造。"""

RuleType = Dict[str, Any]
Instruction = Dict[str, Any]

SecurityLevel = Literal["LOW", "HIGH", "UNKNOWN"]
AuthorityLevel = Literal[
    "HUMAN_APPROVED", "POLICY_APPROVED", "HUMAN_BLOCKED", "POLICY_BLOCKED", "UNKNOWN",
]

# ---------------------------------------------------------------------------
# SecurityType 构造器
# ---------------------------------------------------------------------------


def make_security_type(
    *,
    confidentiality: SecurityLevel = "UNKNOWN",
    trustworthiness: SecurityLevel = "UNKNOWN",
    confidence: SecurityLevel = "UNKNOWN",
    reversible: bool = True,
    authority: AuthorityLevel = "UNKNOWN",
    risk: SecurityLevel = "LOW",
    custom: Optional[Dict[str, Any]] = None,
) -> SecurityType:
    """构建安全属性字典。"""
    return {
        "confidentiality": confidentiality,
        "trustworthiness": trustworthiness,
        "confidence": confidence,
        "reversible": reversible,
        "authority": authority,
        "risk": risk,
        "custom": custom or {},
    }


# ---------------------------------------------------------------------------
# TaintStatus（传播后的信任度 + 机密度）
# ---------------------------------------------------------------------------


class TaintStatus(NamedTuple):
    trustworthiness: SecurityLevel
    confidentiality: SecurityLevel


# ---------------------------------------------------------------------------
# ToolParser 契约
# ---------------------------------------------------------------------------


class ToolParseResult(NamedTuple):
    """工具解析结果：指令类型 + 安全属性。"""

    instruction_type: str
    security_type: SecurityType


ToolParser = Callable[[Dict[str, Any], Optional[TaintStatus]], ToolParseResult]
"""工具解析器签名：(args, taint_status) -> ToolParseResult"""


# ---------------------------------------------------------------------------
# 指令类型 → 分类映射
# ---------------------------------------------------------------------------

INSTRUCTION_TYPE_TO_CATEGORY: Dict[str, str] = {
    # 认知
    "REASON": "COGNITIVE.Reasoning",
    "PLAN": "COGNITIVE.Reasoning",
    "CRITIQUE": "COGNITIVE.Reasoning",
    # 记忆
    "STORE": "MEMORY.Management",
    "RETRIEVE": "MEMORY.Management",
    "COMPRESS": "MEMORY.Management",
    "PRUNE": "MEMORY.Management",
    # 环境 I/O
    "READ": "EXECUTION.Env",
    "WRITE": "EXECUTION.Env",
    "EXEC": "EXECUTION.Env",
    "WAIT": "EXECUTION.Env",
    # 人机交互
    "ASK": "EXECUTION.Human",
    "RESPOND": "EXECUTION.Human",
    "USER_MESSAGE": "EXECUTION.Human",
    # Agent 协作
    "DELEGATE": "EXECUTION.Agent",
    # 感知
    "SUBSCRIBE": "EXECUTION.Perception",
    "RECEIVE": "EXECUTION.Perception",
}


# ---------------------------------------------------------------------------
# 等级排序
# ---------------------------------------------------------------------------

# LOW(0) < UNKNOWN(10) < HIGH(20)
LEVEL_ORDER: Dict[str, float] = {"LOW": 0, "UNKNOWN": 10, "HIGH": 20}


# ---------------------------------------------------------------------------
# 污点传播核心算法（直接复用 ArbiterOS，零改动）
# ---------------------------------------------------------------------------


def compute_prop_taint_for_instruction(
    instructions: List[Dict[str, Any]],
    instr: Dict[str, Any],
) -> TaintStatus:
    """计算单条指令的传播机密度和传播信任度。

    - 纯文本（无 tool_name）：使用自身 confidentiality / trustworthiness。
    - 工具调用：聚合自身 + 所有 reference_tool_id 指向的指令。
      trust = min（最悲观），conf = max（最保守）。
    """
    st = instr.get("security_type")
    if not isinstance(st, dict):
        return TaintStatus(trustworthiness="UNKNOWN", confidentiality="UNKNOWN")

    def _safe_level(v: Any) -> str:
        s = v if isinstance(v, str) else "UNKNOWN"
        return (s or "UNKNOWN").strip() or "UNKNOWN"

    own_conf = _safe_level(st.get("confidentiality"))
    own_trust = _safe_level(st.get("trustworthiness"))

    content = instr.get("content")
    if not isinstance(content, dict) or "tool_name" not in content:
        # 纯文本：用自身值
        return TaintStatus(
            trustworthiness=own_trust if own_trust in LEVEL_ORDER else "UNKNOWN",
            confidentiality=own_conf if own_conf in LEVEL_ORDER else "UNKNOWN",
        )

    # 工具调用：收集需要聚合的 tool_call_id（自身 + reference_tool_id）
    tc_id = content.get("tool_call_id")
    ref_ids = content.get("arguments", {}).get("reference_tool_id") or []
    ids_to_include: set[str] = set()
    if isinstance(tc_id, str) and tc_id.strip():
        ids_to_include.add(tc_id.strip())
    for r in ref_ids if isinstance(ref_ids, list) else []:
        if isinstance(r, str) and r.strip():
            ids_to_include.add(r.strip())

    trust_vals: List[str] = [own_trust]
    conf_vals: List[str] = [own_conf]

    for other in instructions or []:
        if other is instr:
            continue
        oc = other.get("content")
        if not isinstance(oc, dict):
            continue
        o_tc = oc.get("tool_call_id")
        if not isinstance(o_tc, str) or o_tc.strip() not in ids_to_include:
            continue
        ost = other.get("security_type")
        if not isinstance(ost, dict):
            continue
        prop_conf = ost.get("prop_confidentiality") or ost.get("confidentiality")
        prop_trust = ost.get("prop_trustworthiness") or ost.get("trustworthiness")
        if isinstance(prop_conf, str) and prop_conf.strip() in LEVEL_ORDER:
            conf_vals.append(prop_conf.strip())
        if isinstance(prop_trust, str) and prop_trust.strip() in LEVEL_ORDER:
            trust_vals.append(prop_trust.strip())

    raw_trust: SecurityLevel = cast(
        SecurityLevel,
        min(trust_vals, key=lambda v: LEVEL_ORDER.get(v, 0.5)) if trust_vals else "UNKNOWN",
    )
    raw_conf: SecurityLevel = cast(
        SecurityLevel,
        max(conf_vals, key=lambda v: LEVEL_ORDER.get(v, 0.5)) if conf_vals else "UNKNOWN",
    )
    return TaintStatus(trustworthiness=raw_trust, confidentiality=raw_conf)
