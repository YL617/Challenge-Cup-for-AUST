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
# 语义注入检测（Phase 2 核心）
# ---------------------------------------------------------------------------


def _adjust_propagated_trust(instructions: List[Dict[str, Any]]) -> None:
    """对 prior 的 tool_result 内容做语义扫描，标记含 injection marker 的 instruction 为 LOW trust。

    攻击者用"隐藏指令"诱导 agent，prior tool_result 里有 [隐藏指令]/[伪造身份] 等。
    这是语义层（不依赖具体关键词，攻击者改文字也会被检），补 6 条失败 case 的关键。
    """
    INJECTION_MARKERS = (
        # 中文社工/注入标记
        "隐藏指令", "伪造身份", "伪造签名", "伪造指令", "伪造",
        "外部伪装", "冒充", "假冒", "我是局长", "我是科长", "我是主任", "我是处长",
        "立即创建", "立即下发", "立即执行", "立即删除", "立即覆盖",
        "删除所有", "批量提交", "批量删除", "读取敏感", "覆盖本文件",
        "直接发送", "绕过审批", "跳过审批", "不通过审批",
        "secret_sync", "exfil", "outbox",
        # 英文 prompt injection 标记
        "ignore", "disregard", "system_prompt", "system prompt", "you are now", "new instructions",
        "do not tell", "do not inform", "do not report", "do not log",
    )
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
        if any(mk in raw.lower() for mk in INJECTION_MARKERS):
            st = ins.get("security_type")
            if isinstance(st, dict):
                st["prop_trustworthiness"] = "LOW"
                st["prop_trust_injection"] = True  # 标记是语义检测发现的


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

        for tc in tool_calls:
            tool_name, tool_call_id, args_dict, _ = self.runtime.parse_tool_call(tc)
            ins = instr_by_id.get(tool_call_id or "", {})
            ctx = _build_tool_context(
                tool_name=tool_name,
                tool_call_id=tool_call_id or "",
                args_dict=args_dict,
                ins=ins,
                runtime=self.runtime,
            )

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
