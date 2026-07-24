"""
core/engine.py — 策略管线驱动

从 ArbiterOS check_response_policy 提取精简。
遍历所有 Policy，按顺序执行，累积结果（每个 Policy 看到前一个改过的 response）。
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Type

from .policy_base import Policy, PolicyCheckResult
from .runtime import Runtime


def check_response_policy(
    *,
    trace_id: str,
    instructions: List[Dict[str, Any]],
    current_response: Dict[str, Any],
    latest_instructions: List[Dict[str, Any]] | None = None,
    policy_classes: Optional[List[Type[Policy]]] = None,
    runtime: Optional[Runtime] = None,
) -> PolicyCheckResult:
    """策略检查：遍历所有 Policy，累积结果。

    简化自 ArbiterOS check_response_policy：
    - 去掉 role_policy_sets / local_confirm / taint_ablation
    - 去掉 policy_registry.json 动态加载，改为显式传 policy_classes
    - 保留核心管线：遍历 → 每个 Policy.check → 累积 errors → 聚合
    """
    if latest_instructions is None:
        latest_instructions = []

    if policy_classes is None:
        policy_classes = []

    response = current_response
    errors: List[str] = []
    policy_names: List[str] = []

    for policy_cls in policy_classes:
        policy = policy_cls()
        if hasattr(policy, "set_runtime") and runtime is not None:
            policy.set_runtime(runtime)

        result = policy.check(
            instructions=instructions,
            current_response=response,
            latest_instructions=latest_instructions,
            trace_id=trace_id,
        )

        if result.modified:
            response = result.response
            if result.error_type:
                errors.append(result.error_type)
            name = policy_cls.__name__
            if name not in policy_names:
                policy_names.append(name)

    return PolicyCheckResult(
        modified=len(errors) > 0,
        response=response,
        error_type="\n\n".join(errors) if errors else None,
        policy_names=policy_names,
    )
