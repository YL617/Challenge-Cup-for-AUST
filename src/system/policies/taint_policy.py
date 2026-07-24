"""
policies/taint_policy.py — 污点策略（默认启用）

从 ArbiterOS TaintPolicy 提取精简。
判定逻辑：
  input 工具（read/kb_search）：trust >= conf 才放行
  output 工具（write/exec/send_email）：trust >= prop_conf 才放行

这是补 6 条失败 case 的关键——ArbiterOS 默认关闭，我们默认启用。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..core.policy_base import Policy, PolicyCheckResult
from ..core.runtime import Runtime
from ..core.types import LEVEL_ORDER


# 工具分类
_INPUT_TOOLS = frozenset({
    "read", "read_file", "kb_search", "knowledge_base_query",
    "list_emails", "calendar_list_events", "click_link",
    "gmailreademail", "evernotemanagersearch", "todoistsearch",
})

_OUTPUT_TOOLS = frozenset({
    "write", "edit", "save", "file_write", "exec", "terminalexecute",
    "send_email", "send_email_batch", "send_sms",
    "calendar_create", "calendar_create_event", "cron",
    "browser_upload", "submit_form", "share_file", "approve",
})


def _level_at_least(a: Optional[str], b: Optional[str]) -> bool:
    """a >= b？排序：LOW(0) < UNKNOWN(10) < HIGH(20)。"""
    rank_a = LEVEL_ORDER.get((a or "UNKNOWN").strip(), 0.5)
    rank_b = LEVEL_ORDER.get((b or "UNKNOWN").strip(), 0.5)
    return rank_a >= rank_b


def _classify_tool(tool_name: str) -> str:
    """返回 "input" / "output" / "none"。"""
    name = (tool_name or "").strip().lower()
    if name in _OUTPUT_TOOLS:
        return "output"
    if name in _INPUT_TOOLS:
        return "input"
    # 未知工具默认 output（保守）
    return "output"


def _friendly_message(tool_name: str, kind: str, trust: str, required: str) -> str:
    if kind == "input":
        return (
            f"我没有执行工具 `{tool_name}`。\n"
            f"这一步属于读取信息类操作。\n"
            f"当前请求的可信级别为 `{trust}`，而执行这一步至少需要 `{required}`。\n"
            "原因：当前请求的可信级别不足以读取这类信息。\n"
            "如果你希望继续，请改为访问更低敏感度的信息，或先经过更高权限/确认流程。"
        )
    return (
        f"我没有执行工具 `{tool_name}`。\n"
        f"这一步属于输出/修改/外发类操作。\n"
        f"当前请求的可信级别为 `{trust}`，而执行这一步至少需要 `{required}`。\n"
        "原因：当前请求的可信级别不足以进行这类输出或副作用操作。\n"
        "如果你希望继续，请降低操作敏感度，或先完成所需的确认/授权步骤。"
    )


class TaintPolicy(Policy):
    """污点策略：基于 trustworthiness vs confidentiality 门控工具调用。

    与 ArbiterOS TaintPolicy 的区别：默认启用（ArbiterOS 默认关闭）。
    """

    def __init__(self) -> None:
        self._runtime: Optional[Runtime] = None

    def set_runtime(self, runtime: Runtime) -> None:
        self._runtime = runtime

    @property
    def runtime(self) -> Runtime:
        if self._runtime is None:
            self._runtime = Runtime()
        return self._runtime

    def check(
        self,
        instructions: List[Dict[str, Any]],
        current_response: Dict[str, Any],
        latest_instructions: List[Dict[str, Any]],
        trace_id: str,
        **kwargs: Any,
    ) -> PolicyCheckResult:
        response = dict(current_response)
        tool_calls = self.runtime.extract_tool_calls(response)
        if not tool_calls:
            return PolicyCheckResult(modified=False, response=current_response, error_type=None)

        # 建立 tool_call_id → instruction 映射
        instr_by_id: Dict[str, Dict[str, Any]] = {}
        for ins in latest_instructions or []:
            content = ins.get("content")
            if not isinstance(content, dict):
                continue
            tcid = content.get("tool_call_id")
            if isinstance(tcid, str) and tcid:
                instr_by_id[tcid] = ins

        errors: List[str] = []
        kept: List[Dict[str, Any]] = []

        for tc in tool_calls:
            tool_name, tool_call_id, args_dict, _ = self.runtime.parse_tool_call(tc)
            ins = instr_by_id.get(tool_call_id or "", {})
            st = ins.get("security_type") if isinstance(ins, dict) else {}
            if not isinstance(st, dict):
                st = {}

            kind = _classify_tool(tool_name)
            if kind == "none":
                kept.append(tc)
                continue

            def _safe(v: Any) -> str:
                s = v if isinstance(v, str) else "UNKNOWN"
                return (s or "UNKNOWN").strip() or "UNKNOWN"

            trust = _safe(st.get("prop_trustworthiness") or st.get("trustworthiness"))
            conf = _safe(st.get("confidentiality"))
            prop_conf = _safe(st.get("prop_confidentiality") or st.get("confidentiality"))

            if kind == "input":
                ok = _level_at_least(trust, conf)
                required = conf
                reason = f"trustworthiness < confidentiality ({trust} < {conf})"
            else:
                ok = _level_at_least(trust, prop_conf)
                required = prop_conf
                reason = f"trustworthiness < prop_confidentiality ({trust} < {prop_conf})"

            if ok:
                kept.append(tc)
            else:
                errors.append(_friendly_message(tool_name, kind, trust, required))
                self.runtime.audit(
                    phase="policy.taint",
                    trace_id=trace_id,
                    tool=tool_name,
                    decision="BLOCK",
                    reason=reason,
                    args=args_dict,
                )

        if errors:
            response["tool_calls"] = kept if kept else None
            if not kept:
                response["function_call"] = None
                if not isinstance(response.get("content"), str) or not response.get("content"):
                    response["content"] = "\n\n".join(errors[:3])
            return PolicyCheckResult(
                modified=True, response=response, error_type="\n\n".join(errors)
            )

        return PolicyCheckResult(modified=False, response=current_response, error_type=None)
