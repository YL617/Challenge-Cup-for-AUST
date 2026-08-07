"""
mock_tool_server.py — 模拟 OpenClaw 工具调用,响应 LLM 发出的 tool_call。

监听 4001,接受 OpenAI tool_call 协议(简化版):
  POST /tools/invoke  {"tool": "...", "arguments": {...}}
  返回:               {"result": "..."}

支持按 trace_id 上下文返回不同的结果(用于多步 case)。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

PORT = 4001

# 全局状态:当前 trace 的 mock tool 返回序列
# 每个 trace_id 对应一个迭代器
_TRACE_STATE: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()


def load_cases() -> Dict[str, Dict]:
    cases = {}
    # 加载多个版本的 case 文件 (优先级: v3 > v2 > v1)
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
                if trace_id and trace_id not in cases:  # 优先用先加载的(新版本)
                    cases[trace_id] = c
    return cases


CASES = load_cases()


class ToolHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length) or b"{}")

        if path == "/tools/invoke":
            tool_name = body.get("tool", "")
            arguments = body.get("arguments", {})
            trace_id = body.get("trace_id", "")

            # 找到这个 trace 的 mock_tool_path,按 tool_name 返回第一个匹配 step 的结果
            case = CASES.get(trace_id, {})
            mock_path = case.get("mock_tool_path", [])

            # 如果显式给了 step_idx, 用 step_idx
            step_idx = body.get("step_idx")
            if step_idx is not None:
                if step_idx < len(mock_path):
                    result = mock_path[step_idx]["tool_result"]
                else:
                    # 越界时返回默认占位 (避免 LLM 看到错误后误判)
                    result = f"[mock_tool_server] {tool_name} 已执行(无更多数据)"
            else:
                # 否则按 tool_name 找第一个匹配 step
                matched = None
                for step in mock_path:
                    if step["assistant_action"] == tool_name:
                        matched = step
                        break
                if matched:
                    result = matched["tool_result"]
                else:
                    # 没匹配时,根据 tool 类型返回合理默认 (不返回错误避免触发 LLM 警惕)
                    DEFAULT_RESULTS = {
                        "exec": "操作执行成功(无输出)",
                        "send_email": "邮件已发送",
                        "send_email_batch": "批量邮件已发送",
                        "write": "文件已写入",
                        "read": "(文件内容)",
                        "kb_search": "(无搜索结果)",
                        "email_list_inbox": "(收件箱为空)",
                        "calendar_list_events": "(无日程)",
                        "user_list": "(无用户)",
                        "doc_list": "(无文件)",
                        "browser_upload": "上传成功",
                        "approve": "审批通过",
                        "cron": "定时任务已设置",
                    }
                    result = DEFAULT_RESULTS.get(tool_name, f"[mock_tool_server] {tool_name} 已执行")

            response = {"result": result, "tool": tool_name, "trace_id": trace_id}
            self._json_response(200, response)
        elif path == "/reset":
            trace_id = body.get("trace_id", "")
            with _LOCK:
                _TRACE_STATE.pop(trace_id, None)
            self._json_response(200, {"reset": trace_id})
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
        ts = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] {args[0]}")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ToolHandler)
    print(f"🛠️  Mock tool server 启动: http://127.0.0.1:{PORT}")
    print(f"   已加载 {len(CASES)} 条 case 的 mock_tool_path")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 mock tool server stopped")


if __name__ == "__main__":
    main()
