#!/usr/bin/env python3
"""
src/system/proxy/gov_proxy.py — 政务智能体防护代理（OpenAI Chat Completions 版）

支持 stepfun step-3.7-flash（OpenAI 协议）。
  OpenClaw → 本代理(:4000) → stepfun → response → 跑策略引擎 → 返回

工作流程：
  1. 监听 4000，OpenAI Chat Completions 协议
  2. 收到请求 → 透传 stepfun
  3. 收到 response → 提取 tool_calls / 文本 → 跑策略引擎
  4. 判定：allow → 原样返回；deny → 改写 tool_calls / 替换文本
  5. 写审计日志
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 确保能 import 我们的策略引擎
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))

from system.core.runtime import Runtime
from system.policies.unary_gate import INJECTION_MARKERS as _INJECTION_MARKERS
from system.policies.llm_injection_judge import judge as llm_judge

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

PROXY_PORT = 4000
STEPFUN_BASE_URL = "https://api.stepfun.com/step_plan/v1"
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
AUDIT_LOG = Path(__file__).resolve().parent / "audit_log.jsonl"


def _load_env_local():
    """从 .env.local 读环境变量"""
    env_path = Path(__file__).resolve().parents[3] / ".env.local"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _get_api_config(model: str = "") -> Dict:
    """根据 model 名选择 API endpoint + key"""
    _load_env_local()
    if not model:
        model = os.environ.get("PROXY_MODEL", "step-3.7-flash")

    if "deepseek" in model.lower():
        return {
            "url": f"{DEEPSEEK_BASE_URL}/chat/completions",
            "key": os.environ.get("DEEPSEEK_API_KEY", ""),
            "model": model,
            "name": "deepseek",
        }
    else:
        return {
            "url": f"{STEPFUN_BASE_URL}/chat/completions",
            "key": os.environ.get("STEPFUN_API_KEY", "") or _get_stepfun_key_legacy(),
            "model": model,
            "name": "stepfun",
        }


def _get_stepfun_key_legacy() -> Optional[str]:
    """从 OpenClaw sqlite 读 stepfun key (旧版兼容)"""
    db_paths = [
        os.path.expanduser("~/.openclaw/agents/main/agent/openclaw-agent.sqlite"),
        os.path.expanduser("~/Documents/日常工作/openclaw-runtime/.openclaw/agents/main/agent/openclaw-agent.sqlite"),
    ]
    for db in db_paths:
        if not os.path.exists(db):
            continue
        try:
            con = sqlite3.connect(db)
            row = con.execute("SELECT store_json FROM auth_profile_store WHERE store_key='primary'").fetchone()
            if not row:
                continue
            data = json.loads(row[0])
            for pid, prof in data.get("profiles", {}).items():
                if prof.get("key"):
                    return prof["key"]
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 策略检查
# ---------------------------------------------------------------------------


def _extract_user_text(messages: List[Dict]) -> str:
    """从 messages 提取用户最后一条消息文本"""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict) and "text" in b:
                        parts.append(b["text"])
                return " ".join(parts)
    return ""


def _extract_response_text(choice: Dict) -> str:
    """从 OpenAI Chat Completion choice 提取文本"""
    message = choice.get("message", {})
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and "text" in b:
                parts.append(b["text"])
        return " ".join(parts)
    return ""


def _check_response_security(
    response: Dict,
    messages: List[Dict],
) -> Tuple[Dict, List[Dict]]:
    """对 LLM response 跑策略检查。

    Returns:
        (modified_response, audit_entries)
    """
    audit_entries = []
    response = json.loads(json.dumps(response))  # deep copy
    choices = response.get("choices", [])
    if not choices:
        return response, audit_entries
    choice = choices[0]
    message = choice.get("message", {})

    # 1. tool_call 检查
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}

            from system.policies.unary_gate import (
                _evaluate_rules, _build_tool_context, _DEFAULT_RULES, _collect_prior_text, _summarize_args,
            )
            from system.core.tool_parsers import parse_tool_instruction

            parsed = parse_tool_instruction(tool_name, args)
            instruction = {
                "content": {
                    "tool_name": tool_name,
                    "tool_call_id": tc.get("id", ""),
                    "arguments": args,
                },
                "security_type": parsed.security_type,
            }

            prior_text = _collect_prior_text(message.get("security_type", {}).get("_instructions", []), tc.get("id", ""))
            # 简化：从 messages 收集所有 prior text
            all_text = []
            for m in messages:
                content = m.get("content", "")
                if isinstance(content, str):
                    all_text.append(content)
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and "text" in b:
                            all_text.append(b["text"])
            prior_text = "\n".join(all_text)[:3000]

            has_injection = any(
                mk in prior_text.lower() for mk in _INJECTION_MARKERS
            )

            ctx = _build_tool_context(
                tool_name=tool_name,
                tool_call_id=tc.get("id", ""),
                args_dict=args,
                ins=instruction,
                runtime=Runtime(),
                injection_in_trace=has_injection,
            )

            decision = _evaluate_rules(rules=_DEFAULT_RULES, ctx=ctx)
            if decision is None and _INJECTION_MARKERS and prior_text:
                # 没匹配 keyword 但有 prior 文本，调 LLM judge
                decision_str, reason = llm_judge(
                    prior_text=prior_text[:2000],
                    tool_name=tool_name,
                    args_summary=_summarize_args(args),
                    trace_id=response.get("id", "proxy"),
                )
                if decision_str == "BLOCK":
                    from system.policies.unary_gate import RuleDecision
                    decision = RuleDecision(
                        index=0, rule_id="LLM-PROXY-001",
                        title="Proxy LLM judge",
                        description="LLM 判定危险",
                        effect="BLOCK", scope="tool",
                        message=reason or "LLM 判定风险",
                        predicate=None, selector={}, actual={},
                        source="llm_proxy",
                    )
                    audit_entries.append({
                        "type": "llm_judge",
                        "decision": decision_str,
                        "reason": reason[:200],
                    })

            if decision is not None:
                tc["_blocked"] = decision.message

        # 移除被标记的 tool_calls
        original_calls = message.get("tool_calls", [])
        message["tool_calls"] = [tc for tc in original_calls if "_blocked" not in tc]
        blocked = [tc for tc in original_calls if "_blocked" in tc]
        if blocked:
            for tc in blocked:
                audit_entries.append({
                    "type": "tool_call_blocked",
                    "tool": tc.get("function", {}).get("name", ""),
                    "reason": tc.get("_blocked", ""),
                })
            # 加拦截说明到文本
            existing_content = message.get("content", "")
            if existing_content is None:
                existing_content = ""
            block_msg = "\n\n⚠️ 已拦截危险工具调用：" + "、".join(
                [tc.get("function", {}).get("name", "") for tc in blocked]
            ) + "。理由：" + " | ".join([tc.get("_blocked", "")[:50] for tc in blocked])
            message["content"] = existing_content + block_msg

    # 2. 文本内容检查
    text = _extract_response_text(choice)
    user_text = _extract_user_text(messages)
    all_text = text + " " + user_text

    keyword_hit = False
    for marker in _INJECTION_MARKERS:
        if marker in all_text.lower():
            audit_entries.append({
                "type": "text_injection_detected",
                "marker": marker,
                "layer": "keyword",
            })
            keyword_hit = True
            break

    if not text.strip() and keyword_hit:
        audit_entries.append({
            "type": "attack_blocked",
            "layer": "keyword+upstream_refusal",
            "marker": audit_entries[0].get("marker", ""),
            "action": "blocked",
        })
    elif text.strip() and len(text) > 10:
        suspicious_signals = ["删除", "发送", "读取", "exec", "rm ", "curl", "shadow", "password", "key"]
        needs_llm = any(s in text.lower() for s in suspicious_signals)
        if needs_llm or keyword_hit:
            decision, reason = llm_judge(
                prior_text=user_text[:2000] + "\n" + text[:1000],
                tool_name="@text_response",
                args_summary=text[:500],
                trace_id=response.get("id", "proxy"),
            )
            audit_entries.append({
                "type": "llm_judge",
                "decision": decision,
                "reason": reason[:200],
            })
            if decision == "BLOCK":
                if not message.get("content"):
                    message["content"] = ""
                block_msg = f"\n\n⚠️ 已拦截：{reason[:100]}"
                message["content"] = (message["content"] or "") + block_msg
                audit_entries.append({"type": "text_blocked", "reason": reason[:200]})

    return response, audit_entries


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


def _to_openai_sse(response: Dict, is_blocked: bool = False) -> str:
    """OpenAI Chat Completions 流式格式 (SSE)"""
    # 简化：返回单条 choice（OpenClaw 也接受非流式）
    return "data: " + json.dumps(response, ensure_ascii=False) + "\n\n"


class GovProxyHandler(BaseHTTPRequestHandler):
    """HTTP handler：OpenAI Chat Completions proxy"""

    def do_POST(self):
        print(f"\n>>> 收到请求: {self.path}")
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            request_body = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        # 根据请求的 model 选择 API
        req_model = request_body.get("model", "step-3.7-flash")
        api_config = _get_api_config(req_model)
        api_key = api_config["key"]
        if not api_key:
            self.send_error(500, f"No API key for {api_config['name']}")
            return

        messages = request_body.get("messages", [])
        is_stream = request_body.get("stream", False)
        print(f"    model={req_model} ({api_config['name']}) stream={is_stream} msgs={len(messages)}")

        upstream_url = api_config["url"]
        # 确保 model 名正确
        request_body["model"] = api_config["model"]
        req = urllib.request.Request(
            upstream_url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                response_json = json.loads(raw)
        except urllib.error.HTTPError as e:
            error_body = e.read()
            print(f"  ⚠️ stepfun HTTP {e.code}: {error_body[:200]}")
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error_body)
            return
        except Exception as e:
            print(f"  ⚠️ 上游错误: {e}")
            self.send_error(502, f"Upstream error: {e}")
            return

        # 跑策略检查
        modified, audit_entries = _check_response_security(response_json, messages)

        # 写审计日志
        for entry in audit_entries:
            entry["timestamp"] = datetime.now(timezone.utc).isoformat()
            entry["model"] = request_body.get("model", "?")
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            if entry["type"] == "tool_call_blocked":
                print(f"  🚫 BLOCKED: {entry['tool']}")
            elif entry["type"] == "text_injection_detected":
                print(f"  ⚠️  INJECTION: {entry.get('marker', '')}")
            elif entry["type"] == "llm_judge":
                print(f"  🧠 LLM: {entry.get('decision', '')}")

        # 返回
        response_bytes = json.dumps(modified, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_bytes)))
        self.end_headers()
        self.wfile.write(response_bytes)

    def log_message(self, format, *args):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"  [{ts}] {args[0]}")


def main():
    _load_env_local()
    api_config = _get_api_config()
    api_key = api_config["key"]
    if not api_key:
        print(f"❌ 找不到 {api_config['name']} API key")
        sys.exit(1)

    print(f"🛡️  政务智能体防护代理启动中...")
    print(f"   监听: 127.0.0.1:{PROXY_PORT}")
    print(f"   默认上游: {api_config['url']} ({api_config['name']})")
    print(f"   协议: OpenAI Chat Completions")
    print(f"   审计: {AUDIT_LOG}")
    print()

    server = ThreadingHTTPServer(("127.0.0.1", PROXY_PORT), GovProxyHandler)
    print(f"✅ 代理已启动: http://127.0.0.1:{PROXY_PORT}")
    print()
    print("等待请求...")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 代理已停止")
        server.server_close()


if __name__ == "__main__":
    main()
