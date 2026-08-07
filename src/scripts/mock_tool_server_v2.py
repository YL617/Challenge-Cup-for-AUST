"""
mock_tool_server_v2.py — 支持多轮对话的 mock tool server

特性:
  1. 加载 v1/v2/v3 (单轮 case) + v4 (多轮 case)
  2. 按 trace_id + round_idx 追踪 mock 结果
  3. 同一个 round 内, 同一个工具返回对应 mock_result (按调用顺序)
  4. 支持 round 重置 (POST /reset)

工作流 (多轮 case):
  - Round 1: LLM 调用工具 → 返回 mock_results[round=1][0]
  - Round 1: LLM 再次调用工具 → 返回 mock_results[round=1][1]
  - Round 2: LLM 调用工具 → 返回 mock_results[round=2][0]
  - ...

监听: 0.0.0.0:4001
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

PORT = 4001

# 全局状态: trace_id -> {round_idx: int, called_in_round: [(tool, idx)]}
_TRACE_STATE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def load_cases() -> Dict[str, Dict]:
    cases = {}
    # v4 ~ v20 + v2/v3_realistic 多轮 case
    for path in [
        "data/system-design/e2e-pilot/e2e_cases_v6_realistic_23.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v5_realistic_15.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v4_realistic_25.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v3_realistic_50.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v2_realistic.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v20.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v19.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v18.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v17.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v16.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v15.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v14.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v13.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v12.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v11.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v10.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v9.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v8.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_demo_v7.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v6.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v5.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_realistic_v4.jsonl",
    ]:
        p = Path(path)
        if not p.exists():
            continue
        with open(p) as f:
            for line in f:
                c = json.loads(line)
                # v4/v5/v6/v7 用 src_trace_id 作为主键
                tid = c.get("src_trace_id") or c.get("trace_id")
                if tid:
                    cases[tid] = c

    # v3/v2/v1 (回退)
    for path in [
        "data/system-design/e2e-pilot/e2e_cases_v3.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v2.jsonl",
        "data/system-design/e2e-pilot/e2e_cases_v1.jsonl",
    ]:
        p = Path(path)
        if not p.exists():
            continue
        with open(p) as f:
            for line in f:
                c = json.loads(line)
                trace_id = c.get("trace_id", "")
                if trace_id and trace_id not in cases:
                    cases[trace_id] = c
    return cases


CASES = load_cases()


# v1/v2/v3 单轮 case 的 fallback 默认返回
DEFAULT_RESULTS = {
    "exec": "操作执行成功(无输出)",
    "send_email": "邮件已发送",
    "send_email_batch": "批量邮件已发送",
    "write": "文件已写入",
    "read": "(文件内容)",
    "kb_search": "(无搜索结果)",
    "email_list_inbox": "(收件箱为空)",
    "calendar_list_events": "(无日程)",
    "calendar_get_materials": "(无材料)",
    "calendar_create": "会议已创建",
    "user_list": "(无用户)",
    "doc_list": "(无文件)",
    "browser_upload": "上传成功",
    "approve": "审批通过",
    "cron": "定时任务已设置",
    "query_db": "(查询结果)",
    "web_fetch": "(网页内容)",
    "procurement_get_request": "(采购申请不存在)",
}


def get_v4_mock(trace_id: str, tool_name: str) -> Optional[str]:
    """v4 多轮 case 的 mock 结果"""
    case = CASES.get(trace_id)
    if not case or "rounds" not in case:
        return None

    # 计算要走多少个 round 才能找到 mock
    for _ in range(len(case["rounds"]) + 1):
        with _LOCK:
            state = _TRACE_STATE.setdefault(trace_id, {"round_idx": 0, "called_in_round": []})
            called_in_round = state["called_in_round"]
            current_round_idx = state["round_idx"]

            if current_round_idx >= len(case["rounds"]):
                # 所有 round 都用完
                return DEFAULT_RESULTS.get(tool_name, f"[mock] {tool_name} 已执行")

            round_data = case["rounds"][current_round_idx]
            called_count = len(called_in_round)

            if called_count < len(round_data.get("mock_results", [])):
                result = round_data["mock_results"][called_count]
                called_in_round.append(tool_name)
                return result

            # 推进到下一轮
            state["round_idx"] += 1
            state["called_in_round"] = []
            # 循环继续


def get_v1_mock(trace_id: str, tool_name: str) -> Optional[str]:
    """v1/v2/v3 单轮 case 的 mock 结果"""
    case = CASES.get(trace_id)
    if not case or "mock_tool_path" not in case:
        return None

    with _LOCK:
        state = _TRACE_STATE.setdefault(trace_id, {"called": []})
        called = state["called"]

        # 按 mock_tool_path 顺序, 找第一个匹配 tool_name 且未用过的 step
        for i, step in enumerate(case["mock_tool_path"]):
            if step.get("assistant_action") == tool_name and i not in called:
                called.append(i)
                return step.get("tool_result", "")

    return None


class ToolHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(content_length) or b"{}")
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        if path == "/tools/invoke":
            tool_name = body.get("tool", "")
            trace_id = body.get("trace_id", "")

            # 先尝试 v4 (多轮)
            result = get_v4_mock(trace_id, tool_name)
            # 再尝试 v1/v2/v3 (单轮)
            if result is None:
                result = get_v1_mock(trace_id, tool_name)
            # 最后 fallback
            if result is None:
                result = DEFAULT_RESULTS.get(tool_name, f"[mock_tool_server] {tool_name} 已执行")

            response = {"result": result, "tool": tool_name, "trace_id": trace_id}
            self._json_response(200, response)
        elif path == "/reset":
            trace_id = body.get("trace_id", "")
            with _LOCK:
                _TRACE_STATE.pop(trace_id, None)
            self._json_response(200, {"reset": trace_id})
        elif path == "/set_round":
            # 强制设置 round (用于 e2e_runner 主动控制)
            trace_id = body.get("trace_id", "")
            round_idx = body.get("round_idx", 0)
            with _LOCK:
                state = _TRACE_STATE.setdefault(trace_id, {"round_idx": 0, "called_in_round": []})
                state["round_idx"] = round_idx
                state["called_in_round"] = []
            self._json_response(200, {"trace_id": trace_id, "round_idx": round_idx})
        else:
            self.send_error(404, f"Unknown path: {path}")

    def _json_response(self, code: int, body: Dict):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] {args[0]}")


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), ToolHandler)
    print(f"🛠️  Mock tool server v2 启动: http://0.0.0.0:{PORT}")
    print(f"   加载 case 数: {len(CASES)}")
    v4_count = sum(1 for c in CASES.values() if "rounds" in c)
    v1_count = sum(1 for c in CASES.values() if "mock_tool_path" in c)
    print(f"   v4 多轮: {v4_count}, v1/v2/v3 单轮: {v1_count}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 mock tool server v2 stopped")


if __name__ == "__main__":
    main()
