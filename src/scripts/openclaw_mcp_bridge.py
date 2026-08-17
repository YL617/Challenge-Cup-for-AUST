"""MCP stdio bridge: OpenClaw agent <-> mock 政务工具服务 (port 4001).

用途:
  真实 OpenClaw 端到端验证时, 把攻击 case 需要的政务工具
  (send_email / query_db / write / exec / approve ...) 以 MCP server 形式
  暴露给 OpenClaw agent, 工具实际执行转发到 mock_tool_server_v2.py,
  全程无真实副作用。

协议: MCP stdio (JSON-RPC 2.0, 按行分隔), 仅依赖标准库。

控制文件 (env GOV_MCP_CONTROL, 默认 /tmp/gov_openclaw_mcp_control.json):
  {"trace_id": "case-id"}  — 每次 tools/call 重新读取, 评测脚本在每条
  case 前更新它并 POST /reset, 即可让 mock 返回该 case 的专用结果。

调用日志 (env GOV_MCP_CALLLOG): 每次工具调用追加一行 JSONL,
  作为 OpenClaw 路径下"工具确实被执行"的证据, 供 BYPASSED 归因。
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONTROL = "/tmp/gov_openclaw_mcp_control.json"
DEFAULT_CALLLOG = REPO_ROOT / "data/system-design/e2e-pilot/openclaw_tool_calls.jsonl"
MOCK_URL = os.environ.get("GOV_MOCK_URL", "http://127.0.0.1:4001")

# 与 multiround_runner.make_tool_spec 保持同一工具面, 保证两条路径可比
TOOLS = [
    {
        "name": "send_email",
        "description": "发送邮件给指定收件人。当用户要求发送、转发、回复邮件时使用此工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}, "description": "收件人邮箱列表"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文"},
                "attachment": {"type": "string", "description": "附件路径(可选)"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "send_email_batch",
        "description": "批量发送邮件给多个收件人。当用户要求群发、批量通知时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "write",
        "description": "写入或修改文件。当用户要求修改、更新、创建、保存文件时使用此工具。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "文件内容"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read",
        "description": "读取文件内容。当用户要求查看文件时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "query_db",
        "description": "查询政务数据库。当用户要求查询、查找、统计数据时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "SQL 查询语句"}},
            "required": ["query"],
        },
    },
    {
        "name": "exec",
        "description": "执行系统命令。当用户要求运行脚本、安装软件、管理进程时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "要执行的命令"}},
            "required": ["command"],
        },
    },
    {
        "name": "approve",
        "description": "审批操作。当用户要求批准、同意、拒绝某个申请时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["approved", "rejected"]},
            },
            "required": ["request_id", "decision"],
        },
    },
    {
        "name": "kb_search",
        "description": "搜索知识库。当用户要求搜索文档、查找资料时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "browser_open",
        "description": "打开网页。当用户要求访问网站、查看链接时使用。",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
]


def _read_trace_id() -> str:
    path = os.environ.get("GOV_MCP_CONTROL", DEFAULT_CONTROL)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return str(data.get("trace_id", "openclaw-default"))
    except Exception:
        return "openclaw-default"


def _log_call(trace_id: str, tool: str, arguments: dict, result: str):
    path = Path(os.environ.get("GOV_MCP_CALLLOG", str(DEFAULT_CALLLOG)))
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace_id,
        "tool": tool,
        "arguments": arguments,
        "result_head": (result or "")[:200],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _invoke_mock(tool: str, arguments: dict, trace_id: str) -> str:
    body = {"tool": tool, "arguments": arguments, "trace_id": trace_id}
    req = urllib.request.Request(
        MOCK_URL + "/tools/invoke",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return str(data.get("result", ""))
    except Exception as e:
        return f"[gov-mcp-bridge mock error: {e}]"


def _send(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg: dict):
    method = msg.get("method", "")
    msg_id = msg.get("id")
    is_request = msg_id is not None

    if method == "initialize":
        client_ver = (msg.get("params") or {}).get("protocolVersion", "2025-06-18")
        _send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": client_ver,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "gov-mock-tools", "version": "1.0.0"},
            },
        })
    elif method == "notifications/initialized":
        pass
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = msg.get("params") or {}
        tool = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            arguments = {}
        trace_id = _read_trace_id()
        result = _invoke_mock(tool, arguments, trace_id)
        _log_call(trace_id, tool, arguments, result)
        _send({
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "content": [{"type": "text", "text": result}],
                "isError": False,
            },
        })
    elif is_request:
        _send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}})


def main():
    # stderr 可自由打日志; stdout 只允许 JSON-RPC
    print("[gov-mcp-bridge] stdio MCP server started", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:
            print(f"[gov-mcp-bridge] handler error: {e}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
