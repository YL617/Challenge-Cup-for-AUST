"""
core/policy_base.py — 策略基类 + 检查结果

从 ArbiterOS policy/policy.py + policy_check.py 的 PolicyCheckResult 提取。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PolicyCheckResult:
    """策略检查结果。"""

    modified: bool
    """是否修改了 response。"""

    response: Dict[str, Any]
    """返回的 response（原始或修改后）。"""

    error_type: Optional[str] = None
    """修改时的中文拦截原因；None 表示未修改。"""

    policy_names: List[str] = field(default_factory=list)
    """命中策略名列表。"""

    inactivate_error_type: Optional[str] = None
    """observe-only 模式下的"本应拦截"原因。"""


class Policy(ABC):
    """策略基类。子类必须实现 check()。"""

    @abstractmethod
    def check(
        self,
        instructions: List[Dict[str, Any]],
        current_response: Dict[str, Any],
        latest_instructions: List[Dict[str, Any]],
        trace_id: str,
        **kwargs: Any,
    ) -> PolicyCheckResult:
        ...
