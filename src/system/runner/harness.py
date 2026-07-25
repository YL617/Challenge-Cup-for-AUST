"""
runner/harness.py — 离线回放引擎

从 ArbiterOS policy_test_harness.py 提取核心链路：
  case JSON (prior + current) → InstructionBuilder → check_response_policy → 结果

兼容现有 case JSON 格式（trace_id / prior / current / tag）。
不需要 LLM、不需要 OpenClaw——case 里的 prior/current 就是预构造的 tool call。
"""

from __future__ import annotations

import argparse
import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.builder import InstructionBuilder
from ..core.engine import check_response_policy
from ..core.policy_base import PolicyCheckResult
from ..core.runtime import Runtime
from ..core.types import TaintStatus


# ---------------------------------------------------------------------------
# case JSON 解析（兼容 ArbiterOS 格式）
# ---------------------------------------------------------------------------


def _parse_tool_arguments(raw: Any) -> Optional[Dict[str, Any]]:
    """解析工具参数（可能是 dict 或 JSON 字符串）。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _ensure_reference_tool_id(args: Dict[str, Any]) -> Dict[str, Any]:
    """确保参数包含 reference_tool_id（case 格式要求）。"""
    if "reference_tool_id" not in args:
        args["reference_tool_id"] = []
    return args


def _extract_tool_call_details(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 assistant message 提取 tool_call 详情列表。"""
    details = []
    for tc in response.get("tool_calls") or []:
        fn = tc.get("function") or {}
        args = _parse_tool_arguments(fn.get("arguments", "{}")) or {}
        details.append(
            {
                "tool_name": fn.get("name", ""),
                "tool_call_id": tc.get("id"),
                "arguments": args,
            }
        )
    return details


def _apply_response_transform(trace_id: str, assistant_msg: Dict[str, Any]) -> Dict[str, Any]:
    """把 assistant message 规范化为 policy 输入格式。"""
    msg = copy.deepcopy(assistant_msg)
    if msg.get("role") is None:
        msg["role"] = "assistant"
    return msg


# ---------------------------------------------------------------------------
# 回放核心
# ---------------------------------------------------------------------------


def _append_assistant_turn(
    builder: InstructionBuilder, trace_id: str, assistant_msg: Dict[str, Any]
) -> Dict[str, Any]:
    """处理一个 assistant 步骤：解析 tool_calls + 构建 instructions。"""
    for tc_detail in _extract_tool_call_details(assistant_msg):
        args = tc_detail.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        args = _ensure_reference_tool_id(args)
        builder.add_from_tool_call(
            tool_name=tc_detail["tool_name"],
            tool_call_id=tc_detail.get("tool_call_id"),
            arguments=args,
            result=None,
        )
    return _apply_response_transform(trace_id, assistant_msg)


def _append_tool_result_turn(
    builder: InstructionBuilder,
    trace_id: str,
    *,
    tool_name: str,
    tool_call_id: str,
    arguments: Dict[str, Any],
    content: str,
) -> None:
    """处理一个 tool result 步骤。"""
    args = dict(arguments) if isinstance(arguments, dict) else {}
    args = _ensure_reference_tool_id(args)
    builder.add_from_tool_call(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        arguments=args,
        result={"raw": content},
    )


@dataclass
class ReplayOutcome:
    """单条 case 回放结果。"""

    trace_id: str
    case_number: str
    safe_unsafe: str
    policy_result: PolicyCheckResult
    instructions: List[Dict[str, Any]]
    latest_instructions: List[Dict[str, Any]]


def run_policy_replay_from_spec(
    spec: Dict[str, Any],
    policy_classes: List[type],
    runtime: Optional[Runtime] = None,
) -> ReplayOutcome:
    """回放一条 case。

    Args:
        spec: case JSON（含 trace_id / prior / current）
        policy_classes: 要执行的策略类列表
        runtime: 可选的 Runtime 实例（用于审计日志）
    """
    if runtime is None:
        runtime = Runtime()

    trace_id = (spec.get("trace_id") or "policy-test-trace").strip()
    prior = spec.get("prior") or []
    current = spec.get("current") or {}
    case_number = spec.get("case_number", trace_id)
    safe_unsafe = spec.get("safe_unsafe", "unknown")

    # 去掉 tag（harness 专用字段，不影响解析）
    current_msg = copy.deepcopy(current)
    current_msg.pop("tag", None)
    if current_msg.get("role") is None:
        current_msg["role"] = "assistant"

    builder = InstructionBuilder(trace_id=trace_id)

    # 处理 prior
    for step in prior:
        if not isinstance(step, dict):
            continue
        kind = step.get("kind")
        if kind == "assistant":
            msg = copy.deepcopy(step.get("message") or {})
            msg.pop("tag", None)
            _append_assistant_turn(builder, trace_id, msg)
        elif kind == "tool":
            args = step.get("arguments") or {}
            if isinstance(args, str):
                args = _parse_tool_arguments(args) or {}
            result = step.get("result") or step.get("content") or ""
            if not isinstance(result, str):
                result = str(result) if result is not None else ""
            _append_tool_result_turn(
                builder,
                trace_id,
                tool_name=step.get("tool_name", "unknown"),
                tool_call_id=step.get("tool_call_id", ""),
                arguments=args,
                content=result,
            )

    # 处理 current（记录 latest_instructions 的起始位置）
    count_before = len(builder.instructions)
    final_current = _append_assistant_turn(builder, trace_id, current_msg)
    latest_instructions = builder.instructions[count_before:]

    # Phase 2: 语义注入检测（必须在所有 policy 之前）
    from ..policies.unary_gate import _adjust_propagated_trust
    _adjust_propagated_trust(builder.instructions)
    # 强制把 latest_instructions 的 prop_trustworthiness 设为 LOW（如果 trace 里有任何 injection marker）
    has_injection = any(
        (ins.get("security_type") or {}).get("prop_trust_injection")
        for ins in builder.instructions
    )
    if has_injection:
        for ins in latest_instructions:
            st = ins.get("security_type")
            if isinstance(st, dict):
                st["prop_trustworthiness"] = "LOW"

    # 策略检查
    policy_result = check_response_policy(
        trace_id=trace_id,
        instructions=builder.instructions,
        current_response=final_current,
        latest_instructions=latest_instructions,
        policy_classes=policy_classes,
        runtime=runtime,
    )

    return ReplayOutcome(
        trace_id=trace_id,
        case_number=case_number,
        safe_unsafe=safe_unsafe,
        policy_result=policy_result,
        instructions=builder.instructions,
        latest_instructions=latest_instructions,
    )


# ---------------------------------------------------------------------------
# 批量回放 + 指标计算
# ---------------------------------------------------------------------------


@dataclass
class BatchResult:
    """批量回放结果。"""

    total: int
    # 混淆矩阵（unsafe=正类，blocked=True=预测正类）
    tp: int  # unsafe 被拦截
    tn: int  # safe 被放行
    fp: int  # safe 被误拦
    fn: int  # unsafe 被放行（漏检）
    errors: int  # 执行出错
    per_case: List[Dict[str, Any]]

    @property
    def accuracy(self) -> float:
        denom = self.tp + self.tn + self.fp + self.fn
        return (self.tp + self.tn) / denom if denom else 0.0

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 0.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def pass_rate(self) -> float:
        """通过率 = (TN + TP) / total（宽松口径，warning 也算 pass）"""
        return (self.tn + self.tp) / self.total if self.total else 0.0


def run_batch(
    cases: List[Dict[str, Any]],
    policy_classes: List[type],
    runtime: Optional[Runtime] = None,
) -> BatchResult:
    """批量回放 case 列表，计算混淆矩阵。"""
    if runtime is None:
        runtime = Runtime()

    tp = tn = fp = fn = errors = 0
    per_case = []

    for spec in cases:
        try:
            outcome = run_policy_replay_from_spec(spec, policy_classes, runtime)
            modified = outcome.policy_result.modified
            expected_unsafe = outcome.safe_unsafe == "unsafe"

            if expected_unsafe and modified:
                tp += 1
                decision = "blocked"
            elif expected_unsafe and not modified:
                fn += 1
                decision = "passed"
            elif not expected_unsafe and modified:
                fp += 1
                decision = "blocked"
            else:
                tn += 1
                decision = "passed"

            per_case.append(
                {
                    "case_number": outcome.case_number,
                    "trace_id": outcome.trace_id,
                    "safe_unsafe": outcome.safe_unsafe,
                    "decision": decision,
                    "modified": modified,
                    "error_type": outcome.policy_result.error_type,
                    "policy_names": outcome.policy_result.policy_names,
                }
            )
        except Exception as e:
            errors += 1
            per_case.append(
                {
                    "case_number": spec.get("case_number", "?"),
                    "trace_id": spec.get("trace_id", "?"),
                    "safe_unsafe": spec.get("safe_unsafe", "?"),
                    "decision": "error",
                    "error": str(e),
                }
            )

    return BatchResult(
        total=len(cases),
        tp=tp,
        tn=tn,
        fp=fp,
        fn=fn,
        errors=errors,
        per_case=per_case,
    )


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="离线回放引擎：用 case JSON 测策略")
    parser.add_argument("--cases", type=Path, required=True, help="case JSONL 文件路径")
    parser.add_argument("--policies", nargs="*", default=[], help="策略类全限定名（如 system.policies.unary_gate.UnaryGatePolicy）")
    parser.add_argument("--output", type=Path, default=None, help="输出 JSON 报告路径")
    args = parser.parse_args(argv)

    # 加载策略类
    policy_classes = []
    for cls_path in args.policies:
        module_path, cls_name = cls_path.rsplit(".", 1)
        import importlib
        mod = importlib.import_module(module_path)
        policy_classes.append(getattr(mod, cls_name))

    # 加载 case
    cases = []
    for line in args.cases.read_text(encoding="utf-8").strip().split("\n"):
        if line.strip():
            cases.append(json.loads(line))

    # 回放
    result = run_batch(cases, policy_classes)

    # 报告
    report = {
        "total": result.total,
        "tp": result.tp,
        "tn": result.tn,
        "fp": result.fp,
        "fn": result.fn,
        "errors": result.errors,
        "accuracy": f"{result.accuracy:.4f}",
        "precision": f"{result.precision:.4f}",
        "recall": f"{result.recall:.4f}",
        "f1": f"{result.f1:.4f}",
        "pass_rate": f"{result.pass_rate:.4f}",
        "failed_cases": [c for c in result.per_case if c["decision"] in ("passed",) and c["safe_unsafe"] == "unsafe"],
        "fp_cases": [c for c in result.per_case if c["decision"] == "blocked" and c["safe_unsafe"] == "safe"],
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
