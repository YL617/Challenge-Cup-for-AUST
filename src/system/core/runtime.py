"""
core/runtime.py — Runtime 类（替代 ArbiterOS 全局 RUNTIME）

ArbiterOS 用模块级全局 RUNTIME 单例，耦合 litellm_callback。
我们用类封装，构造函数注入，方便测试和适配多平台。
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Optional, Tuple


class Runtime:
    """运行时上下文：工具调用提取、参数解析、审计日志。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        self.cfg = cfg or {}
        self._audit_log: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 工具调用提取（OpenAI 兼容格式）
    # ------------------------------------------------------------------

    def extract_tool_calls(self, response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 response 中提取 tool_calls 列表。

        支持 OpenAI 格式：response["tool_calls"] = [{id, type, function: {name, arguments}}]
        """
        tcs = response.get("tool_calls")
        if isinstance(tcs, list):
            return tcs
        return []

    def parse_tool_call(
        self, tc: Dict[str, Any]
    ) -> Tuple[str, Optional[str], Dict[str, Any], bool]:
        """解析单个 tool_call。

        返回 (tool_name, tool_call_id, args_dict, was_json_str)。
        """
        fn = tc.get("function") or {}
        tool_name = fn.get("name", "")
        tool_call_id = tc.get("id")
        raw_args = fn.get("arguments", {})
        was_json_str = False
        if isinstance(raw_args, str):
            was_json_str = True
            try:
                args_dict = json.loads(raw_args)
                if not isinstance(args_dict, dict):
                    args_dict = {}
            except (json.JSONDecodeError, TypeError):
                args_dict = {}
        elif isinstance(raw_args, dict):
            args_dict = raw_args
        else:
            args_dict = {}
        return tool_name, tool_call_id, args_dict, was_json_str

    def write_back_tool_args(
        self, tc: Dict[str, Any], args: Dict[str, Any], was_json_str: bool
    ) -> Dict[str, Any]:
        """把规范化后的 args 写回 tool_call。"""
        new_tc = copy.deepcopy(tc)
        fn = new_tc.setdefault("function", {})
        if was_json_str:
            fn["arguments"] = json.dumps(args, ensure_ascii=False)
        else:
            fn["arguments"] = args
        return new_tc

    # ------------------------------------------------------------------
    # 审计日志
    # ------------------------------------------------------------------

    def audit(
        self,
        *,
        phase: str,
        trace_id: str,
        tool: str,
        decision: str,
        reason: str,
        args: Optional[Dict[str, Any]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """记录一条审计日志。"""
        entry = {
            "phase": phase,
            "trace_id": trace_id,
            "tool": tool,
            "decision": decision,
            "reason": reason,
            "args": args or {},
        }
        if extra:
            entry["extra"] = extra
        self._audit_log.append(entry)

    def get_audit_log(self) -> List[Dict[str, Any]]:
        return list(self._audit_log)

    def clear_audit_log(self) -> None:
        self._audit_log.clear()

    # ------------------------------------------------------------------
    # 工具名规范化（简化的 ArbiterOS canonicalize）
    # ------------------------------------------------------------------

    # 工具名别名（ArbiterOS UG 规则用的规范化名）
    _ALIASES = {
        "file_read": "read",
        "file_write": "write",
        "file_delete": "delete",
        "edit": "write",
    }

    def canonical_tool_name(self, name: str) -> str:
        """规范化工具名（小写 + 别名展开）。"""
        return self._ALIASES.get(name.lower(), name.lower())
