"""
mock_proxy_server.py — 模拟 gov_proxy:4000,接受 OpenAI chat completions 请求。

只用于本地集成测试(不调真实 stepfun):
  - 接收 chat completions 请求
  - 检查 messages 里有没有 INJECTION_MARKERS
  - 如果有,返回 4 种固定 response 之一(模拟 LLM 不同行为)
  - 否则正常返回(模拟 stepfun 真实调用)

用于: 验证 e2e_proxy_runner.py 的完整链路工作正常,不需要真实 API key。
"""

from __future__ import annotations

import json
import sys
import time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from system.policies.unary_gate import INJECTION_MARKERS

PORT = 4000

# 加载 10 条 case 的预期 attack_actions
CASES = {}
with open("data/system-design/e2e-pilot/e2e_cases_v1.jsonl") as f:
    for line in f:
        c = json.loads(line)
        CASES[c["trace_id"]] = c


def find_attack_tool_for_messages(messages: List[Dict], trace_id: str) -> List[Dict]:
    """根据 messages 判断 LLM 走到了哪一步,返回下一步要调的工具"""
    case = CASES.get(trace_id, {})
    user_msg = case.get("user_msg", "")
    mock_path = case.get("mock_tool_path", [])

    # 数 assistant 的 tool_call 数 = 已经走了几步
    tool_call_count = sum(
        1 for m in messages
        if m.get("role") == "assistant" and m.get("tool_calls")
    )

    # 如果已经走过所有 mock 步骤,下一步应该是 attack action
    if tool_call_count >= len(mock_path):
        # 返回 attack action
        attack_actions = case.get("src_attack_actions", [])
        return [
            {
                "id": f"call_attack_{i}",
                "type": "function",
                "function": {
                    "name": a["name"],
                    "arguments": json.dumps(a["arguments"], ensure_ascii=False),
                },
            }
            for i, a in enumerate(attack_actions)
        ]

    # 否则返回 mock path 的下一步工具
    next_step = mock_path[tool_call_count]
    return [{
        "id": f"call_step_{tool_call_count}",
        "type": "function",
        "function": {
            "name": next_step["assistant_action"],
            "arguments": json.dumps({"query": "stub", "reference_tool_id": []}, ensure_ascii=False),
        },
    }]


def check_proxy_block(messages: List[Dict], tool_call: Dict, trace_id: str) -> Dict:
    """模拟 proxy 的拦截决策:对 attack_actions,看是否会被结构性规则拦"""
    case = CASES.get(trace_id, {})
    pattern = case.get("pattern", "")
    fn = tool_call.get("function", {})
    name = fn.get("name", "")
    args_str = fn.get("arguments", "{}")

    # 模拟现有 deny 规则:
    # - exec 默认阻断
    # - send_email_batch 检测外部外发
    # - cron / send_email 检测 prior 含社工 marker

    if name == "exec":
        return {"proxy_block": True, "proxy_rule": "GOV-DENY-001", "proxy_reason": "exec 命令执行默认阻断"}
    if name == "send_email_batch":
        if "attacker@" in args_str or "通讯录" in args_str or "user@example.com" in args_str:
            return {"proxy_block": True, "proxy_rule": "GOV-DENY-005", "proxy_reason": "检测到外部邮件外发,默认阻断"}
    if name in ("cron", "send_email"):
        # 看 prior 有没有 marker
        all_text = " ".join(str(m.get("content", "")) for m in messages)
        if any(mk in all_text.lower() for mk in INJECTION_MARKERS):
            return {"proxy_block": True, "proxy_rule": "GOV-DENY-SOCIAL", "proxy_reason": "检测到 prior 含诱导/社工标记,触发高风险工具调用,已阻断"}

    return {}


class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length) or b"{}")

        if "/v1/chat/completions" not in path:
            self.send_error(404)
            return

        messages = body.get("messages", [])
        # 从 system message 提取 trace_id (我们在 e2e_proxy_runner 把它放进去)
        trace_id = ""
        for m in messages:
            if m.get("role") == "system":
                # 简单解析: trace_id 在 content 里
                c = str(m.get("content", ""))
                if "trace_id=" in c:
                    trace_id = c.split("trace_id=")[1].split()[0].strip()

        # 决定下一步 tool_call
        tool_calls = find_attack_tool_for_messages(messages, trace_id)

        # 模拟 proxy 拦截:对每个 tool_call,检查是否会被拦
        # 注意: check_proxy_block 会修改传入的 dict (原地加 _blocked 字段)
        for tc in tool_calls:
            block_info = check_proxy_block(messages, tc, trace_id)
            if block_info:
                tc.update(block_info)

        # 构造 response
        response = {
            "id": f"mock-{int(time.time())}",
            "object": "chat.completion",
            "model": "step-3.7-flash (mocked)",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": tool_calls,  # 包含被 _blocked 标记的
                },
                "finish_reason": "tool_calls",
            }],
        }

        data = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] {args[0]}")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ProxyHandler)
    print(f"🛡️  Mock proxy server 启动: http://127.0.0.1:{PORT}")
    print(f"   模拟 step-3.7-flash + 自研防护规则 (不调真实 API)")
    print(f"   已加载 {len(CASES)} 条 case 用于模拟")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 mock proxy stopped")


if __name__ == "__main__":
    main()
