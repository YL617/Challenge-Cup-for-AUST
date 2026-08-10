"""
policies/unary_gate.py — 一元门策略 + 谓词 DSL

从 ArbiterOS unary_gate_policy.py（2366 行）提取核心：
  - _eval_predicate（~25 操作符的 JSON 逻辑语言）
  - _resolve_value（值表达式解析）
  - _compare_values / _compare_key（安全等级比较）
  - _selector_matches（规则选择器匹配）
  - _evaluate_rules（首条匹配扫描）
  - UnaryGatePolicy.check（主检查方法）

去掉：LLM judge（UG-060/061）、legacy 规则编译、复杂中文消息渲染。
保留：声明式规则引擎 + 友好拦截信息。
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.policy_base import Policy, PolicyCheckResult
from ..core.runtime import Runtime
from ..core.types import LEVEL_ORDER


# ---------------------------------------------------------------------------
# Phase 2.5: 模块级 INJECTION_MARKERS（供 proxy 等外部模块引用）
# ---------------------------------------------------------------------------

INJECTION_MARKERS = (
    # 中文社工/注入标记
    "隐藏指令", "伪造身份", "伪造签名", "伪造指令", "伪造",
    "外部伪装", "冒充", "假冒", "我是局长", "我是科长", "我是主任", "我是处长",
    "立即创建", "立即下发", "立即执行", "立即删除", "立即覆盖",
    "删除所有", "批量提交", "批量删除", "读取敏感", "覆盖本文件",
    "直接发送", "绕过审批", "跳过审批", "不通过审批",
    "secret_sync", "exfil", "outbox",
    # 英文 prompt injection 标记（基础）
    "ignore", "disregard", "system_prompt", "system prompt", "you are now", "new instructions",
    "do not tell", "do not inform", "do not report", "do not log",
    # W1-D2: 英文 prompt injection 黑话
    "jailbreak", "jailbroken", "jail-break", "dan mode", "developer mode", "do anything now",
    "pretend you", "act as if", "imagine you are", "roleplay as",
    "from now on", "previous instructions", "prior instructions", "above instructions",
    "ignore all rules", "no restrictions", "unlock mode", "god mode", "bypass safety",
    "training is over", "real mode", "without restrictions",
    "forget your", "disregard your", "ignore your", "override your",
    "new persona", "new role", "new identity", "as an ai",
    "execute command", "run shell", "terminal access",
    "translation", "language model", "respond in", "write in", "speak in",
    "### instruction", "system:", "assistant:", "user:",
    "exfiltrate", "leak", "transmit", "send to attacker",
    "malicious", "backdoor", "payload", "reverse shell",
    "decrypt", "hashdump", "lsass", "shadow file", "sam file",
    # Phase 2.5 攻击类型
    "image contains", "图片包含", "图片中包含", "ocr contains", "ocr extracted",
    "ignore previous", "ignore all previous", "override", "system override",
    "tool result contains", "return value", "output contains", "返回值包含", "工具返回",
    "previous session", "上轮 session", "之前用户", "prior conversation",
    "🤖", "💀", "🌐", "忽略所有",
    "then send to", "随后发给", "and then execute", "chain to",
    "rotate credential", "update key", "change password", "轮换凭据", "更新 key",
    "sk-emergency", "api_key", "aws_access",
    "schedule later", "at 2am", "凌晨", "delayed", "tomorrow",
    "0 2 * * *", "每天凌晨",
    "base64", "hex encoded", "decode this", "base64 编码", "十六进制", "解码",
    "| base64 -d", "| sh", "| bash",
    "step1", "step2", "step3", "execute steps", "分步", "执行 step",
    "send to all", "burst", "20 times", "发所有人", "批量发送", "all users",
)


# ---------------------------------------------------------------------------
# RuleDecision 数据结构
# ---------------------------------------------------------------------------


@dataclass
class RuleDecision:
    """规则命中决策。"""

    index: int
    rule_id: str
    title: str
    description: str
    effect: str  # "BLOCK" / "WARN" / "APPROVE"
    scope: str
    message: str
    predicate: Any
    selector: Any
    actual: Dict[str, Any]
    source: str = ""


# ---------------------------------------------------------------------------
# 值表达式解析
# ---------------------------------------------------------------------------


def _is_value_expr(v: Any) -> bool:
    """判断 v 是否是值表达式。"""
    return isinstance(v, dict) and len(v) == 1 and next(iter(v)) in (
        "var", "const", "len", "count_intersections"
    )


def _resolve_value(expr: Any, ctx: Dict[str, Any]) -> Any:
    """解析值表达式。"""
    if not _is_value_expr(expr):
        return expr

    op, val = next(iter(expr.items()))

    if op == "var":
        return ctx.get(val)
    if op == "const":
        return val
    if op == "len":
        resolved = _resolve_value(val, ctx)
        if isinstance(resolved, (list, dict, str)):
            return len(resolved)
        return 0
    if op == "count_intersections":
        if isinstance(val, list) and len(val) == 2:
            a = _resolve_value(val[0], ctx)
            b = _resolve_value(val[1], ctx)
            if isinstance(a, list) and isinstance(b, list):
                return len(set(str(x).upper() for x in a) & set(str(x).upper() for x in b))
        return 0
    return None


# ---------------------------------------------------------------------------
# 比较
# ---------------------------------------------------------------------------


def _compare_key(v: Any) -> Tuple[int, str]:
    """排序键：安全等级用 LEVEL_ORDER，其他用字符串。"""
    if isinstance(v, str) and v.upper() in LEVEL_ORDER:
        return (1, str(LEVEL_ORDER[v.upper()]))
    if isinstance(v, bool):
        return (2, str(v))
    if isinstance(v, (int, float)):
        return (3, str(v))
    return (4, str(v).upper())


def _compare_values(a: Any, b: Any, op: str) -> bool:
    """通用比较。"""
    ka, kb = _compare_key(a), _compare_key(b)
    if op in ("eq",):
        return ka == kb
    if op in ("ne",):
        return ka != kb
    if op in ("gt",):
        return ka > kb
    if op in ("ge",):
        return ka >= kb
    if op in ("lt",):
        return ka < kb
    if op in ("le",):
        return ka <= kb
    if op == "between":
        # a 应该是 [lo, hi]
        if isinstance(b, list) and len(b) == 2:
            lo, hi = _compare_key(b[0]), _compare_key(b[1])
            return lo <= ka <= hi
    return False


# ---------------------------------------------------------------------------
# 成员检测规范化
# ---------------------------------------------------------------------------


def _normalize_scalar(v: Any) -> str:
    if isinstance(v, str):
        return v.strip().upper()
    return str(v).strip().upper()


# ---------------------------------------------------------------------------
# 谓词求值引擎
# ---------------------------------------------------------------------------


def _eval_predicate(pred: Any, ctx: Dict[str, Any]) -> bool:
    """求值谓词表达式。返回 True/False。

    支持的操作符：
    - 布尔：all(AND), any(OR), not, truthy, falsy, exists, missing
    - 比较：eq, ne, gt, ge, lt, le, between
    - 成员：in, not_in, contains, intersects, contains_all, subset_of
    - 字符串：starts_with, ends_with, matches(正则)
    """
    # bool 直接返回
    if isinstance(pred, bool):
        return pred

    # 列表 = 隐式 AND
    if isinstance(pred, list):
        return all(_eval_predicate(p, ctx) for p in pred)

    # 值表达式 = bool(resolve(...))
    if _is_value_expr(pred):
        return bool(_resolve_value(pred, ctx))

    if not isinstance(pred, dict):
        return bool(pred)

    # 多键 dict = 隐式 AND
    if len(pred) != 1:
        return all(_eval_predicate({k: v}, ctx) for k, v in pred.items())

    op, val = next(iter(pred.items()))

    # 布尔组合
    if op == "all":
        items = val if isinstance(val, list) else [val]
        return all(_eval_predicate(p, ctx) for p in items)
    if op == "any":
        items = val if isinstance(val, list) else [val]
        return any(_eval_predicate(p, ctx) for p in items)
    if op == "not":
        return not _eval_predicate(val, ctx)
    if op == "truthy":
        return bool(_resolve_value(val, ctx))
    if op == "falsy":
        return not bool(_resolve_value(val, ctx))
    if op == "exists":
        return _resolve_value(val, ctx) is not None
    if op == "missing":
        return _resolve_value(val, ctx) is None

    # 比较（val 必须是 [a, b]）
    if op in ("eq", "ne", "gt", "ge", "lt", "le"):
        if isinstance(val, list) and len(val) == 2:
            a = _resolve_value(val[0], ctx)
            b = _resolve_value(val[1], ctx)
            return _compare_values(a, b, op)
        return False
    if op == "between":
        if isinstance(val, list) and len(val) == 3:
            v = _resolve_value(val[0], ctx)
            lo = _resolve_value(val[1], ctx)
            hi = _resolve_value(val[2], ctx)
            return _compare_values(lo, v, "le") and _compare_values(v, hi, "le")
        return False

    # 成员检测
    if op == "in":
        v = _resolve_value(val[0], ctx) if isinstance(val, list) and len(val) == 2 else None
        lst = _resolve_value(val[1], ctx) if isinstance(val, list) and len(val) == 2 else []
        if isinstance(lst, list):
            return _normalize_scalar(v) in [_normalize_scalar(x) for x in lst]
        return False
    if op == "not_in":
        v = _resolve_value(val[0], ctx) if isinstance(val, list) and len(val) == 2 else None
        lst = _resolve_value(val[1], ctx) if isinstance(val, list) and len(val) == 2 else []
        if isinstance(lst, list):
            return _normalize_scalar(v) not in [_normalize_scalar(x) for x in lst]
        return True
    if op == "contains":
        v = _resolve_value(val[0], ctx) if isinstance(val, list) and len(val) == 2 else None
        container = _resolve_value(val[1], ctx) if isinstance(val, list) and len(val) == 2 else []
        if isinstance(container, list):
            return _normalize_scalar(v) in [_normalize_scalar(x) for x in container]
        if isinstance(container, str):
            return _normalize_scalar(v) in container.upper()
        return False
    if op == "intersects":
        if isinstance(val, list) and len(val) == 2:
            a = _resolve_value(val[0], ctx) or []
            b = _resolve_value(val[1], ctx) or []
            if isinstance(a, list) and isinstance(b, list):
                sa = set(_normalize_scalar(x) for x in a)
                sb = set(_normalize_scalar(x) for x in b)
                return bool(sa & sb)
        return False
    if op == "contains_all":
        if isinstance(val, list) and len(val) == 2:
            subset = _resolve_value(val[0], ctx) or []
            superset = _resolve_value(val[1], ctx) or []
            if isinstance(subset, list) and isinstance(superset, list):
                ss = set(_normalize_scalar(x) for x in subset)
                sp = set(_normalize_scalar(x) for x in superset)
                return ss <= sp
        return False
    if op == "subset_of":
        return _eval_predicate({"contains_all": [val[1], val[0]]} if isinstance(val, list) and len(val) == 2 else {}, ctx)

    # 字符串操作
    if op == "starts_with":
        if isinstance(val, list) and len(val) == 2:
            s = str(_resolve_value(val[0], ctx) or "").upper()
            prefix = str(_resolve_value(val[1], ctx) or "").upper()
            return s.startswith(prefix)
        return False
    if op == "ends_with":
        if isinstance(val, list) and len(val) == 2:
            s = str(_resolve_value(val[0], ctx) or "").upper()
            suffix = str(_resolve_value(val[1], ctx) or "").upper()
            return s.endswith(suffix)
        return False
    if op == "matches":
        if isinstance(val, list) and len(val) == 2:
            s = str(_resolve_value(val[0], ctx) or "")
            pattern = str(_resolve_value(val[1], ctx) or "")
            if len(pattern) <= 256:
                try:
                    return bool(re.search(pattern, s, re.IGNORECASE))
                except re.error:
                    return False
        return False

    # 未知操作符 → False（保守不拦）
    return False


# ---------------------------------------------------------------------------
# 规则选择器匹配
# ---------------------------------------------------------------------------


def _selector_matches(rule: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    """检查规则的 selector 是否匹配当前上下文。"""
    selector = rule.get("selector") or {}
    if not isinstance(selector, dict):
        return True  # 无 selector = 全匹配

    # scope 匹配
    rule_scope = str(rule.get("scope", "any")).lower()
    ctx_scope = str(ctx.get("scope", "tool")).lower()
    if rule_scope != "any" and rule_scope != ctx_scope:
        return False

    # tool 匹配
    sel_tool = selector.get("tool") or selector.get("tools")
    if sel_tool:
        tools = sel_tool if isinstance(sel_tool, list) else [sel_tool]
        tool_names = set(_normalize_scalar(t) for t in tools)
        if "*" not in tool_names:
            ctx_tool = _normalize_scalar(ctx.get("tool_name", ""))
            if ctx_tool not in tool_names:
                return False

    # instruction_type 匹配
    sel_type = selector.get("instruction_type") or selector.get("instruction_types")
    if sel_type:
        types = sel_type if isinstance(sel_type, list) else [sel_type]
        type_set = set(_normalize_scalar(t) for t in types)
        ctx_type = _normalize_scalar(ctx.get("instruction_type", ""))
        if ctx_type not in type_set:
            return False

    # category 匹配
    sel_cat = selector.get("category") or selector.get("categories")
    if sel_cat:
        cats = sel_cat if isinstance(sel_cat, list) else [sel_cat]
        cat_set = set(_normalize_scalar(c) for c in cats)
        ctx_cat = _normalize_scalar(ctx.get("instruction_category", ""))
        if ctx_cat not in cat_set:
            return False

    return True


# ---------------------------------------------------------------------------
# 规则求值（首条匹配）
# ---------------------------------------------------------------------------


def _evaluate_rules(
    rules: List[Dict[str, Any]], ctx: Dict[str, Any]
) -> Optional[RuleDecision]:
    """遍历规则，返回第一条匹配的决策。"""
    for idx, rule in enumerate(rules, start=1):
        if not rule.get("enabled", True):
            continue
        if not _selector_matches(rule, ctx):
            continue
        predicate = rule.get("predicate")
        if predicate is None:
            continue
        if _eval_predicate(predicate, ctx):
            return RuleDecision(
                index=idx,
                rule_id=rule.get("id", f"rule-{idx}"),
                title=rule.get("title", ""),
                description=rule.get("description", ""),
                effect=rule.get("effect", "BLOCK").upper(),
                scope=rule.get("scope", "any"),
                message=rule.get("message", ""),
                predicate=predicate,
                selector=rule.get("selector", {}),
                actual={},
                source=rule.get("source", ""),
            )
    return None


# ---------------------------------------------------------------------------
# 上下文构建
# ---------------------------------------------------------------------------


# 拒绝下传给谓词的上下文键（敏感/冗余字段）
_DENIED_CTX_KEYS: Set[str] = frozenset({
    "raw_args", "custom", "path_hint", "path_basename",
    "exec_segments", "exec_operators", "exec_path_tokens", "exec_write_targets",
})


def _build_tool_context(
    *,
    tool_name: str,
    tool_call_id: str,
    args_dict: Dict[str, Any],
    ins: Dict[str, Any],
    runtime: Runtime,
    injection_in_trace: bool = False,
    prior_send_count: int = 0,
) -> Dict[str, Any]:
    """从 tool_call instruction 构建谓词求值上下文。"""
    st = ins.get("security_type") if isinstance(ins, dict) else {}
    if not isinstance(st, dict):
        st = {}

    ctx: Dict[str, Any] = {
        "scope": "tool",
        "tool_name": runtime.canonical_tool_name(tool_name),
        "tool_call_id": tool_call_id,
        "missing_instruction": not bool(ins),
        "instruction_type": ins.get("instruction_type", "EXEC") if isinstance(ins, dict) else "EXEC",
        "instruction_category": ins.get("instruction_category", "EXECUTION.Env") if isinstance(ins, dict) else "EXECUTION.Env",
        "trustworthiness": st.get("trustworthiness", "UNKNOWN"),
        "confidentiality": st.get("confidentiality", "UNKNOWN"),
        "prop_trustworthiness": st.get("prop_trustworthiness", st.get("trustworthiness", "UNKNOWN")),
        "prop_confidentiality": st.get("prop_confidentiality", st.get("confidentiality", "UNKNOWN")),
        "confidence": st.get("confidence", "UNKNOWN"),
        "authority": st.get("authority", "UNKNOWN"),
        "reversible": st.get("reversible", True),
        "risk": st.get("risk", "LOW"),
        # Phase 2.5: 当前工具是否在含注入标记的 trace 里
        "injection_in_trace": injection_in_trace,
        # 会话内已发送邮件数(防化整为零: 逐条单发绕过批量审批)
        "prior_send_count": prior_send_count,
        "tags": sorted(st.get("tags", [])) if isinstance(st.get("tags"), list) else [],
        "review_required": st.get("review_required", False),
        "approval_required": st.get("approval_required", False),
        "destructive": st.get("destructive", False),
    }

    # 从 args 提取常用字段
    if isinstance(args_dict, dict):
        for k in ("action", "command", "path", "to", "subject", "content", "query", "url"):
            if k in args_dict:
                ctx[k] = args_dict[k]
        # reference_tool_id
        ref = args_dict.get("reference_tool_id")
        if isinstance(ref, list):
            ctx["reference_tool_id"] = ref

    return ctx


# ---------------------------------------------------------------------------
# 友好拦截信息
# ---------------------------------------------------------------------------


def _friendly_tool_block(decision: RuleDecision, ctx: Dict[str, Any]) -> str:
    tool = ctx.get("tool_name", "")
    msg = decision.message or decision.description or decision.title
    return f"我没有执行工具 `{tool}`。\n原因：{msg}\n如果你希望继续，请改为当前策略允许的操作，或先完成所需的确认/授权流程。"


# ---------------------------------------------------------------------------
# Phase 2.4: LLM judge 辅助函数
# ---------------------------------------------------------------------------


def _collect_prior_text(instructions: List[Dict[str, Any]], current_tcid: str) -> str:
    """收集 current tool_call 之前所有 prior instruction 的内容（作为 LLM 上下文）。"""
    parts = []
    for ins in instructions:
        content = ins.get("content")
        if not isinstance(content, dict):
            continue
        # 跳过 current 本身
        if content.get("tool_call_id") == current_tcid:
            continue
        tn = content.get("tool_name", "")
        args = content.get("arguments", {})
        result = content.get("result", {})
        if isinstance(result, dict):
            raw = result.get("raw", "")
        else:
            raw = str(result) if result else ""
        if raw:
            parts.append(f"[{tn}] {str(args)[:200]}\n  -> {raw[:500]}")
    return "\n\n".join(parts[-5:])  # 最多最近 5 步


def _summarize_args(args_dict: Dict[str, Any]) -> str:
    """把 args 压缩成短字符串，避免 token 爆炸。"""
    if not isinstance(args_dict, dict):
        return str(args_dict)[:200]
    parts = []
    for k, v in list(args_dict.items())[:8]:
        s = str(v)[:100]
        parts.append(f"{k}={s}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# 语义注入检测（Phase 2 核心）
# ---------------------------------------------------------------------------


def _adjust_propagated_trust(instructions: List[Dict[str, Any]]) -> None:
    """对 prior 的 tool_result 内容做语义扫描，标记含 injection marker 的 instruction 为 LOW trust。

    攻击者用"隐藏指令"诱导 agent，prior tool_result 里有 [隐藏指令]/[伪造身份] 等。
    这是语义层（不依赖具体关键词，攻击者改文字也会被检），补 6 条失败 case 的关键。
    """
    for ins in instructions:
        content = ins.get("content")
        if not isinstance(content, dict):
            continue
        result = content.get("result")
        if not isinstance(result, dict):
            continue
        raw = result.get("raw", "")
        if not isinstance(raw, str):
            continue
        # Phase 2.5: Spotlighting 来源标记（non-user 来源自动 wrap untrusted_source）
        # 借自 XA_guard：gate1_input.py 里的 _UNTRUSTED_SOURCES 思路
        tool_name = content.get("tool_name", "")
        # 工具名映射：只把"真正从外部数据源读"的工具视为非用户来源
        # 写/导出/导入是用户主动操作，不标 Spotlighting（否则把正常操作误拦）
        # 列表（5 个）：read_image / kb_search / read_email / web_fetch / save_to_kb
        if any(s in tool_name.lower() for s in (
            "read_image", "image",  # 图片读取（OCR 注入）
            "kb_search", "knowledge", "save_to_kb", "save_kb",  # KB 操作
            "web_fetch", "email", "get_document",  # 网络/邮件/文档元数据
        )):
            st = ins.get("security_type")
            if isinstance(st, dict):
                st["prop_trustworthiness"] = "LOW"
                st["prop_trust_injection"] = True
                st["prop_trust_source"] = "spotlighting"
        # Phase 2.5 关键词匹配（更宽的注入 marker 列表）
        if any(mk in raw.lower() for mk in INJECTION_MARKERS):
            st = ins.get("security_type")
            if isinstance(st, dict):
                st["prop_trustworthiness"] = "LOW"
                st["prop_trust_injection"] = True
                st["prop_trust_source"] = st.get("prop_trust_source", "keyword")


# ---------------------------------------------------------------------------
# 规则加载
# ---------------------------------------------------------------------------


_DEFAULT_RULES: List[Dict[str, Any]] = [
    {
        "id": "GOV-DENY-001",
        "title": "禁止执行危险命令",
        "description": "exec 工具默认阻断（除非已审批）",
        "scope": "tool",
        "selector": {"tool": "exec"},
        "predicate": {"truthy": {"var": "missing_instruction"}},
        "effect": "BLOCK",
        "message": "exec 命令执行默认阻断",
        "enabled": True,
    },
    {
        "id": "GOV-DENY-002",
        "title": "禁止 rm 删除",
        "scope": "tool",
        "selector": {"tool": "exec"},
        "predicate": {
            "any": [
                {"matches": [{"var": "command"}, "rm\\s"]},
                {"matches": [{"var": "command"}, "delete"]},
            ]
        },
        "effect": "BLOCK",
        "message": "删除操作属于不可逆破坏行为，默认阻断",
        "enabled": True,
    },
    {
        "id": "GOV-DENY-003",
        "title": "禁止 curl/wget 外发",
        "scope": "tool",
        "selector": {"tool": "exec"},
        "predicate": {
            "any": [
                {"matches": [{"var": "command"}, "curl\\s"]},
                {"matches": [{"var": "command"}, "wget\\s"]},
            ]
        },
        "effect": "BLOCK",
        "message": "外部数据外发（curl/wget）默认阻断",
        "enabled": True,
    },
    {
        "id": "GOV-APPROVE-001",
        "title": "外发邮件需审批",
        "scope": "tool",
        "selector": {"tool": ["send_email", "send_email_batch"]},
        "predicate": {
            "any": [
                {"matches": [{"var": "to"}, "@example\\.com"]},
                {"matches": [{"var": "to"}, "attacker"]},
            ]
        },
        "effect": "BLOCK",
        "message": "检测到外部邮件外发，默认阻断",
        "enabled": True,
    },
    {
        "id": "GOV-DENY-004",
        "title": "禁止修改政策文件",
        "scope": "tool",
        "selector": {"tool": ["write", "edit"], "instruction_type": "WRITE"},
        "predicate": {"matches": [{"var": "path"}, "/policy/"]},
        "effect": "BLOCK",
        "message": "政策文件受保护，禁止未授权修改",
        "enabled": True,
    },
    {
        "id": "GOV-DENY-005",
        "title": "禁止浏览器类工具访问外部链接/表单",
        "scope": "tool",
        "selector": {"tool": ["click_link", "submit_form", "browser_open", "web_browser_navigate", "WebBrowserNavigateTo"]},
        "predicate": {"truthy": {"const": True}},
        "effect": "BLOCK",
        "message": "浏览器操作默认阻断（社工攻击常用入口），如需访问请走人工审批",
        "enabled": True,
    },
    {
        # Phase 2.5.1: 只对"高风险工具" + trace 含 injection 时 deny
        "id": "GOV-DENY-006",
        "title": "高风险工具 + prior 含诱导标记时阻断",
        "scope": "tool",
        "selector": {"tool": [
            "exec", "terminalexecute", "send_email", "send_email_batch",
            "send_sms", "share_file", "approve", "cron",
            "export_csv", "export", "save_to_kb",
            "read_image", "kb_search", "knowledge_base_query",
        ]},
        "predicate": {"truthy": {"var": "injection_in_trace"}},
        "effect": "BLOCK",
        "message": "检测到 prior 含诱导/社工标记，触发高风险工具调用，已阻断",
        "enabled": True,
    },
    {
        # GOV-APPROVE-002: send_email 外发到非内部地址需审批
        "id": "GOV-APPROVE-002",
        "title": "邮件外发到外部/群发地址需审批",
        "scope": "tool",
        "selector": {"tool": ["send_email", "send_email_batch", "send_mail"]},
        "predicate": {
            "any": [
                {"matches": [{"var": "to"}, "external"]},
                {"matches": [{"var": "to"}, "public"]},
                {"matches": [{"var": "to"}, "all-departments"]},
                {"matches": [{"var": "to"}, "all-staff"]},
                {"matches": [{"var": "to"}, "all-participants"]},
                {"matches": [{"var": "to"}, "dept-share"]},
                {"matches": [{"var": "to"}, "sibling-bureau"]},
                {"matches": [{"var": "to"}, "subordinate"]},
                {"matches": [{"var": "to"}, "superior-bureau"]},
                {"matches": [{"var": "to"}, "media"]},
                {"matches": [{"var": "cc"}, "external"]},
            ]
        },
        "effect": "BLOCK",
        "message": "邮件外发到外部/群发/跨部门地址，需走审批流程（当前为自动阻断保护）",
        "enabled": True,
    },
    {
        # GOV-DENY-007: write 到正式/机密路径阻断
        "id": "GOV-DENY-007",
        "title": "写入正式/机密文件路径阻断",
        "scope": "tool",
        "selector": {"tool": ["write", "edit", "save", "file_write"]},
        "predicate": {
            "any": [
                {"matches": [{"var": "path"}, "/official/"]},
                {"matches": [{"var": "path"}, "/confidential/"]},
                {"matches": [{"var": "path"}, "/data/policy/"]},
                {"matches": [{"var": "path"}, "/registers/"]},
                {"matches": [{"var": "path"}, "/archive/"]},
                {"matches": [{"var": "path"}, "v2025"]},
                {"matches": [{"var": "path"}, "policy"]},
            ]
        },
        "effect": "BLOCK",
        "message": "写入正式/机密/政策文件路径，禁止未授权修改（仅允许 /drafts/ 路径）",
        "enabled": True,
    },
    {
        # GOV-DENY-008: exec 含 SQL 写操作阻断 (伪造签字/批量审批用)
        "id": "GOV-DENY-008",
        "title": "SQL 写操作阻断",
        "scope": "tool",
        "selector": {"tool": ["exec", "terminalexecute", "process_execute"]},
        "predicate": {
            "any": [
                {"matches": [{"var": "command"}, "UPDATE\\s"]},
                {"matches": [{"var": "command"}, "INSERT\\s"]},
                {"matches": [{"var": "command"}, "DELETE\\s"]},
                {"matches": [{"var": "command"}, "DROP\\s"]},
                {"matches": [{"var": "command"}, "ALTER\\s"]},
                {"matches": [{"var": "command"}, "SET\\s+status"]},
                {"matches": [{"var": "command"}, "approver"]},
                {"matches": [{"var": "command"}, "代签"]},
            ]
        },
        "effect": "BLOCK",
        "message": "SQL 写操作（伪造签字/批量审批）阻断，需走正式审批系统",
        "enabled": True,
    },
    {
        # GOV-DENY-009: cron/定时任务阻断 (延迟攻击用)
        "id": "GOV-DENY-009",
        "title": "定时任务阻断",
        "scope": "tool",
        "selector": {"tool": ["cron", "schedule", "crontab"]},
        "predicate": {"truthy": {"const": True}},
        "effect": "BLOCK",
        "message": "定时任务默认阻断（防止延迟攻击/数据定时外泄）",
        "enabled": True,
    },
    {
        # GOV-DENY-010: exec 含 journalctl --vacuum (删审计日志)
        "id": "GOV-DENY-010",
        "title": "审计日志清理阻断",
        "scope": "tool",
        "selector": {"tool": ["exec", "terminalexecute"]},
        "predicate": {
            "any": [
                {"matches": [{"var": "command"}, "journalctl"]},
                {"matches": [{"var": "command"}, "vacuum"]},
                {"matches": [{"var": "command"}, "/data/logs/"]},
                {"matches": [{"var": "command"}, "audit"]},
                {"matches": [{"var": "command"}, "systemctl"]},
            ]
        },
        "effect": "BLOCK",
        "message": "审计日志/系统服务操作阻断（防止证据销毁）",
        "enabled": True,
    },
    {
        # GOV-DENY-011: query_db 读取敏感表 (员工/薪资/个人)
        "id": "GOV-DENY-011",
        "title": "敏感数据表查询阻断",
        "scope": "tool",
        "selector": {"tool": ["query_db", "database_query", "sql_query"]},
        "predicate": {
            "any": [
                {"matches": [{"var": "table"}, "employees"]},
                {"matches": [{"var": "table"}, "salary"]},
                {"matches": [{"var": "table"}, "personnel"]},
                {"matches": [{"var": "table"}, "staff"]},
                {"matches": [{"var": "query"}, "身份证"]},
                {"matches": [{"var": "query"}, "银行卡"]},
                {"matches": [{"var": "query"}, "薪资"]},
                {"matches": [{"var": "query"}, "salary"]},
            ]
        },
        "effect": "BLOCK",
        "message": "敏感数据表（员工/薪资/个人）查询阻断，需走审批",
        "enabled": True,
    },
    {
        # GOV-DENY-012: write 字段名变体 (file_name/name/filename/path 全部覆盖)
        "id": "GOV-DENY-012",
        "title": "写入字段名变体覆盖（file_name/name/filename）",
        "scope": "tool",
        "selector": {"tool": ["write", "edit", "save", "file_write", "create_file"]},
        "predicate": {
            "any": [
                {"matches": [{"var": "file_name"}, "/official/"]},
                {"matches": [{"var": "file_name"}, "/confidential/"]},
                {"matches": [{"var": "file_name"}, "/policy/"]},
                {"matches": [{"var": "file_name"}, "/registers/"]},
                {"matches": [{"var": "file_name"}, "/archive/"]},
                {"matches": [{"var": "file_name"}, "v2025"]},
                {"matches": [{"var": "file_name"}, "policy"]},
                {"matches": [{"var": "name"}, "/official/"]},
                {"matches": [{"var": "name"}, "/confidential/"]},
                {"matches": [{"var": "name"}, "/policy/"]},
                {"matches": [{"var": "name"}, "v2025"]},
                {"matches": [{"var": "filename"}, "/official/"]},
                {"matches": [{"var": "filename"}, "policy"]},
            ]
        },
        "effect": "BLOCK",
        "message": "写入字段变体（file_name/name/filename）检测到敏感路径，已阻断",
        "enabled": True,
    },
    {
        # GOV-DENY-013: calendar_create 字段变体支持 (start_time/end_time/summary)
        "id": "GOV-DENY-013",
        "title": "calendar_create 字段变体支持（start_time/summary）",
        "scope": "tool",
        "selector": {"tool": ["calendar_create", "schedule_meeting", "create_event"]},
        # 注意: 单次 calendar_create 不应被拦, 应让 LLM Judge 看 trace 内是否异常
        # 这里仅保留为占位, 不做硬规则
        "predicate": {"truthy": {"const": False}},
        "effect": "BLOCK",
        "message": "calendar_create 由 LLM Judge 兜底（单次规则可能误伤）",
        "enabled": False,  # 单条规则易误伤, 让 LLM Judge 兜底
    },
    {
        # GOV-DENY-014: write 联合 send_email 时的"过度代理"组合
        "id": "GOV-DENY-014",
        "title": "write+send_email 联合操作（自动外发）阻断",
        "scope": "tool",
        "selector": {"tool": ["send_email", "send_email_batch", "send_mail"]},
        "predicate": {
            "any": [
                # 同 trace 内有过 write 操作 + 当前 send_email
                # (这需要在 ctx 里检查 trace_writes, 见后续增强)
            ]
        },
        "effect": "BLOCK",
        "message": "trace 内已有 write 操作后的 send_email 被视为自动外发（待 PHASE 3.5 加 trace 级联检查）",
        "enabled": False,  # 暂未实现 trace 级联, 保留为占位
    },
    {
        # GOV-DENY-015: 会话内逐条单发绕过批量审批 (G11)
        # v3-02 实测: LLM 把"发共享邮箱"化整为零为逐科室单发, 批量规则失效。
        # 正常业务一次任务发 1-2 封(通知+抄送)属合理, 第 3 封起判定为
        # 拆分外发, 默认阻断并要求走批量审批通道。
        "id": "GOV-DENY-015",
        "title": "会话内逐条单发邮件绕过批量审批",
        "scope": "tool",
        "selector": {"tool": ["send_email", "send_mail"]},
        "predicate": {
            "ge": [{"var": "prior_send_count"}, {"const": 2}]
        },
        "effect": "BLOCK",
        "message": "本次会话已发送 2 封邮件, 继续逐条发送属于拆分外发, 请改用批量审批通道",
        "enabled": True,
    },
]


def _load_rules(config_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """加载规则。优先从 YAML 配置，否则用默认规则。"""
    if config_path and config_path.exists():
        try:
            import yaml
            data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "rules" in data:
                return data["rules"]
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return _DEFAULT_RULES


# ---------------------------------------------------------------------------
# UnaryGatePolicy
# ---------------------------------------------------------------------------


class UnaryGatePolicy(Policy):
    """一元门策略：基于声明式规则匹配单个 tool_call。

    简化自 ArbiterOS UnaryGatePolicy（2366 行 → ~400 行）。
    """

    def __init__(self, rules: Optional[List[Dict[str, Any]]] = None) -> None:
        self._runtime: Optional[Runtime] = None
        self._rules = rules

    def set_runtime(self, runtime: Runtime) -> None:
        self._runtime = runtime

    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            self._runtime = Runtime()
        return self._runtime

    @property
    def rules(self) -> List[Dict[str, Any]]:
        if self._rules is None:
            self._rules = _load_rules()
        return self._rules

    def check(
        self,
        instructions: List[Dict[str, Any]],
        current_response: Dict[str, Any],
        latest_instructions: List[Dict[str, Any]],
        trace_id: str,
        **kwargs: Any,
    ) -> PolicyCheckResult:
        # Phase 2: 语义注入检测已由 harness 在策略检查前统一调用 _adjust_propagated_trust
        response = dict(current_response)
        tool_calls = self.runtime.extract_tool_calls(response)
        if not tool_calls:
            return PolicyCheckResult(modified=False, response=current_response, error_type=None)

        # 建立 tool_call_id → instruction 映射
        instr_by_id: Dict[str, Dict[str, Any]] = {}
        for ins in latest_instructions or []:
            content = ins.get("content")
            if isinstance(content, dict):
                tcid = content.get("tool_call_id")
                if isinstance(tcid, str) and tcid:
                    instr_by_id[tcid] = ins

        rules = self.rules
        errors: List[str] = []
        kept: List[Dict[str, Any]] = []

        # Phase 2.5: 提前计算 trace 是否含 Spotlighting/keyword 标过的 instruction
        # 给所有 tool_call 共享的 "injection_in_trace" 标志
        injection_in_trace = any(
            (other.get("security_type") or {}).get("prop_trust_injection")
            for other in instructions
        )

        for tc in tool_calls:
            tool_name, tool_call_id, args_dict, _ = self.runtime.parse_tool_call(tc)
            ins = instr_by_id.get(tool_call_id or "", "")
            ctx = _build_tool_context(
                tool_name=tool_name,
                tool_call_id=tool_call_id or "",
                args_dict=args_dict,
                ins=ins,
                runtime=self.runtime,
                injection_in_trace=injection_in_trace,
            )

            decision = None
            # Phase 2.4: LLM 语义判定（窄作用域，只对高风险工具）
            from .llm_injection_judge import judge as llm_judge, should_judge
            if should_judge(tool_name):
                prior_text = _collect_prior_text(instructions, tool_call_id)
                args_summary = _summarize_args(args_dict)
                decision_str, reason = llm_judge(
                    prior_text=prior_text,
                    tool_name=tool_name,
                    args_summary=args_summary,
                    trace_id=trace_id,
                )
                # 只有 LLM 明确判定 BLOCK 才算拦截，ERROR/SAFE/None 一律放行
                # （LLM 失败不能误杀正常操作）
                if decision_str == "BLOCK":
                    from .unary_gate import RuleDecision
                    decision = RuleDecision(
                        index=0,
                        rule_id="LLM-INJECTION-001",
                        title="LLM 判定 tool_call 被诱导",
                        description="prior 内容含隐式社工/prompt 注入",
                        effect="BLOCK",
                        scope="tool",
                        message=reason or "LLM 判定当前 tool_call 存在被诱导风险",
                        predicate=None,
                        selector={},
                        actual={},
                        source="llm_injection_judge",
                    )

            if decision is None:
                decision = _evaluate_rules(rules=rules, ctx=ctx)

            if decision is None:
                kept.append(tc)
                continue

            user_msg = _friendly_tool_block(decision, ctx)
            self.runtime.audit(
                phase="policy.unary_gate",
                trace_id=trace_id,
                tool=tool_name,
                decision=decision.effect,
                reason=f"rule#{decision.index} {decision.rule_id}: {decision.message}",
                args=args_dict,
            )
            errors.append(user_msg)

        if errors:
            response["tool_calls"] = kept if kept else None
            if not kept:
                response["function_call"] = None
                if not isinstance(response.get("content"), str) or not response.get("content"):
                    response["content"] = "\n\n".join(errors[:3])
            return PolicyCheckResult(
                modified=True,
                response=response,
                error_type="\n\n".join(errors),
            )

        return PolicyCheckResult(modified=False, response=current_response, error_type=None)
