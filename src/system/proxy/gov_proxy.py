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
from system.policies.llm_injection_judge import judge as llm_judge, should_judge
from system.policies.intent_classifier import classify_tool_call, intent_to_disposition

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

    环境变量 GOV_PROXY_OBSERVE=1 时为观察模式: 策略照跑、审计照写,
    但不修改响应(不移除 tool_calls、不替换文本) —— 用于"无防护 OpenClaw"
    的攻击有效性测量, 保证链路与有防护时完全一致。
    """
    audit_entries = []
    response = json.loads(json.dumps(response))  # deep copy
    observe_only = os.environ.get("GOV_PROXY_OBSERVE", "") == "1"
    choices = response.get("choices", [])
    if not choices:
        return response, audit_entries
    choice = choices[0]
    message = choice.get("message", {})

    # 1. tool_call 检查
    tool_calls = message.get("tool_calls", [])
    if tool_calls:
        # 统计会话内已发送邮件数(历史 assistant 消息 + 本响应中已处理的),
        # 供 GOV-DENY-015 识别逐条单发绕过批量审批
        send_names = {"send_email", "send_mail"}
        prior_send_count = 0
        for m in messages:
            if m.get("role") == "assistant":
                for htc in m.get("tool_calls", []) or []:
                    hist_name = htc.get("function", {}).get("name", "")
                    hist_name = hist_name.split("__", 1)[-1] if "__" in hist_name else hist_name
                    if hist_name in send_names:
                        prior_send_count += 1
        for tc in tool_calls:
            fn = tc.get("function", {})
            tool_name = fn.get("name", "")
            args_str = fn.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}

            # MCP 客户端 (如 OpenClaw) 会给工具名加 "{server}__" 前缀,
            # 策略规则按政务工具规范名匹配, 评估前先归一化;
            # tc 本身保留原名, 客户端才能继续执行
            policy_tool_name = tool_name.split("__", 1)[-1] if "__" in tool_name else tool_name

            from system.policies.unary_gate import (
                _evaluate_rules, _build_tool_context, _DEFAULT_RULES, _collect_prior_text, _summarize_args,
            )
            from system.core.tool_parsers import parse_tool_instruction

            parsed = parse_tool_instruction(policy_tool_name, args)
            instruction = {
                "content": {
                    "tool_name": policy_tool_name,
                    "tool_call_id": tc.get("id", ""),
                    "arguments": args,
                },
                "security_type": parsed.security_type,
            }

            prior_text = _collect_prior_text(message.get("security_type", {}).get("_instructions", []), tc.get("id", ""))
            # prior_text 保留全量上下文(供 LLM judge 参考);
            # has_injection 只扫不可信来源(user 输入 + tool 结果),
            # 排除 system/assistant 消息 —— 真实 agent 框架(如 OpenClaw)的
            # system prompt 含日期/指令等常规词, 混入扫描会把所有高风险工具
            # 误判为"trace 含注入"造成系统性误拦
            # tool_call_id -> (tool_name, args): 用于判定 tool 结果的来源
            tool_call_meta = {}
            for m in messages:
                if m.get("role") != "assistant":
                    continue
                for t in m.get("tool_calls") or []:
                    fn = t.get("function", {})
                    try:
                        targs = json.loads(fn.get("arguments", "{}")) if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
                    except json.JSONDecodeError:
                        targs = {}
                    tool_call_meta[t.get("id", "")] = (fn.get("name", ""), targs)

            def _is_trusted_tool_result(m: Dict) -> bool:
                """读取技能库(read skills/)属于开发者可信内容;
                读业务文档/查询结果仍是不可信来源(间接注入入口)"""
                meta = tool_call_meta.get(m.get("tool_call_id", ""))
                if not meta:
                    return False
                tname, targs = meta
                tname = tname.split("__", 1)[-1] if "__" in tname else tname
                if tname in ("read", "skills", "skill_read", "load_skill"):
                    path = str(targs.get("path", "") or targs.get("file_path", ""))
                    if "skills/" in path.replace("\\", "/"):
                        return True
                return False

            all_text = []
            untrusted_text = []
            for m in messages:
                content = m.get("content", "")
                role = m.get("role", "")
                texts = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and "text" in b:
                            texts.append(b["text"])
                all_text.extend(texts)
                if role == "user":
                    untrusted_text.extend(texts)
                elif role == "tool" and not _is_trusted_tool_result(m):
                    untrusted_text.extend(texts)
            prior_text = "\n".join(all_text)[:3000]

            has_injection = any(
                mk in "\n".join(untrusted_text).lower() for mk in _INJECTION_MARKERS
            )
            if has_injection:
                # 审计记录命中的具体 marker + 命中消息的角色/摘要, 便于排查误报来源
                untrusted_joined = "\n".join(untrusted_text).lower()
                per_msg = []
                for m in messages:
                    if m.get("role") not in ("user", "tool"):
                        continue
                    c = m.get("content", "")
                    texts = [c] if isinstance(c, str) else [
                        b.get("text", "") for b in c
                        if isinstance(b, dict) and "text" in b
                    ] if isinstance(c, list) else []
                    joined = " ".join(texts).lower()
                    hit = [mk for mk in _INJECTION_MARKERS if mk in joined][:3]
                    if hit:
                        per_msg.append({
                            "role": m.get("role"),
                            "head": joined[:100],
                            "markers": hit,
                        })
                audit_entries.append({
                    "type": "injection_markers_hit",
                    "tool": policy_tool_name,
                    "markers": [
                        mk for mk in _INJECTION_MARKERS
                        if mk in untrusted_joined
                    ][:5],
                    "sources": per_msg[:3],
                })

            ctx = _build_tool_context(
                tool_name=policy_tool_name,
                tool_call_id=tc.get("id", ""),
                args_dict=args,
                ins=instruction,
                runtime=Runtime(),
                injection_in_trace=has_injection,
                prior_send_count=prior_send_count,
            )
            # 本响应内连续 send_email 也计入累计
            if policy_tool_name in send_names:
                prior_send_count += 1

            # 意图分类器 → 处置分层 (L0 放行 / L1 上报 / L2 确认 / L3 阻断)
            intent, _ = classify_tool_call(policy_tool_name, args, prior_send_count)
            disposition = intent_to_disposition(intent)
            origin = "detected"  # detected=真实检测 / degraded=基础设施故障降级
            reason = ""
            rule_id = ""

            if disposition == "L3":
                rule_id = f"INTENT-{intent}"
                reason = f"已阻断: 危险操作意图({intent})"
            else:
                # 结构性规则引擎对 L0/L1/L2 仍全量评估 (GOV-DENY 命中 = 确定性 L3)
                rule_decision = _evaluate_rules(rules=_DEFAULT_RULES, ctx=ctx)
                if rule_decision is not None:
                    disposition = "L3"
                    rule_id = getattr(rule_decision, "rule_id", "RULE")
                    reason = f"已阻断: {rule_decision.message[:80]}"
                elif disposition == "L2" and should_judge(policy_tool_name):
                    # L2 半合法操作送语义层精化: 明确安全→L0, 确认攻击(≥8)→L3,
                    # 其余维持 L2 待人工确认; judge 故障→降级 L2 (degraded)
                    judge_context = "\n".join(untrusted_text)[-2000:]
                    decision_str, judge_reason = llm_judge(
                        prior_text=judge_context,
                        tool_name=policy_tool_name,
                        args_summary=_summarize_args(args),
                        trace_id=response.get("id", "proxy"),
                    )
                    audit_entries.append({
                        "type": "llm_judge",
                        "decision": decision_str,
                        "reason": judge_reason[:200],
                    })
                    if "fail-closed" in judge_reason or "judge_error" in judge_reason:
                        # 基础设施故障不计为攻击拦截 (口径分离)
                        origin = "degraded"
                    elif decision_str == "BLOCK":
                        m = re.search(r"score=(\d+)", judge_reason)
                        score = int(m.group(1)) if m else 6
                        if score >= 8:
                            disposition = "L3"
                            rule_id = "JUDGE-CONFIRM"
                            reason = f"已阻断: 语义确认攻击({judge_reason[:60]})"
                        # score 6-7 维持 L2 待人工确认
                    elif decision_str == "SAFE":
                        m = re.search(r"score=(\d+)", judge_reason)
                        score = int(m.group(1)) if m else 5
                        if score <= 2:
                            disposition = "L0"  # 明确安全的显式请求放行
                # L1 侦察: 放行 + 上报, 不做进一步检查 (链条交给 L3 逮)

            if disposition == "L1":
                audit_entries.append({
                    "type": "tier1_reported",
                    "tool": policy_tool_name,
                    "intent": intent,
                    "origin": "detected",
                })
            elif disposition in ("L2", "L3") and not observe_only:
                tc["_blocked"] = reason or f"需人工确认: {intent}"
                tc["_blocked_rule"] = rule_id
                tc["_tier"] = disposition
                tc["_origin"] = origin
            elif disposition in ("L2", "L3") and observe_only:
                # 观察模式: 不拦, 只记 "如果拦截会拦什么"
                audit_entries.append({
                    "type": "observe_would_block",
                    "tool": policy_tool_name,
                    "tier": disposition,
                    "rule": rule_id,
                    "reason": (reason or f"需人工确认: {intent}")[:120],
                })

        # 移除被标记的 tool_calls (L2 确认 / L3 阻断 都暂停执行)
        original_calls = message.get("tool_calls", [])
        message["tool_calls"] = [tc for tc in original_calls if "_blocked" not in tc]
        blocked = [tc for tc in original_calls if "_blocked" in tc]
        if blocked:
            for tc in blocked:
                audit_entries.append({
                    "type": "tool_call_blocked",
                    "tier": tc.get("_tier", ""),
                    "origin": tc.get("_origin", "detected"),
                    "tool": tc.get("function", {}).get("name", ""),
                    "reason": tc.get("_blocked", ""),
                })
            # 响应顶层扩展字段, 供评测 runner 归因。
            # tool_calls 数组保持删除被拦项, 防止真实客户端执行;
            # OpenAI 协议客户端会忽略未知顶层字段。
            response["x_gov_proxy"] = {
                "blocked_tool_calls": [
                    {
                        "tool": tc.get("function", {}).get("name", ""),
                        "args": tc.get("function", {}).get("arguments", ""),
                        "rule": tc.get("_blocked_rule", ""),
                        "tier": tc.get("_tier", ""),
                        "origin": tc.get("_origin", "detected"),
                        "reason": tc.get("_blocked", ""),
                    }
                    for tc in blocked
                ],
            }
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
            if decision == "BLOCK" and "fail-closed" in reason:
                # 文本层 judge 故障: 降级放行并记录, 不计为攻击拦截 (口径分离)
                audit_entries.append({"type": "judge_degraded", "reason": reason[:120]})
            elif decision == "BLOCK" and not observe_only:
                # 替换原文而非追加, 防止泄密内容照常返回给客户端
                message["content"] = f"⚠️ 该响应已被防护系统拦截。理由:{reason[:100]}"
                audit_entries.append({
                    "type": "text_blocked",
                    "origin": "detected",
                    "reason": reason[:200],
                })
                # 设置 x_gov_proxy 扩展字段（供 runner 归因）
                if "x_gov_proxy" not in response:
                    response["x_gov_proxy"] = {"blocked_tool_calls": []}
                response["x_gov_proxy"]["text_blocked"] = {
                    "reason": reason[:200],
                }
            elif decision == "BLOCK" and observe_only:
                audit_entries.append({
                    "type": "observe_would_block_text",
                    "reason": reason[:120],
                })

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
        if os.environ.get("GOV_PROXY_DEBUG_MSG"):
            # 临时诊断: 打印消息结构 (角色/工具名/内容开头), 不进审计日志
            for i, m in enumerate(messages):
                tc_names = [
                    f"{t.get('function', {}).get('name', '')}({str(t.get('function', {}).get('arguments', ''))[:60]})"
                    for t in (m.get("tool_calls") or [])
                ]
                head = str(m.get("content", ""))[:70].replace("\n", "␤")
                print(f"    msg[{i}] role={m.get('role')} "
                      f"tool_call_id={m.get('tool_call_id','-')} tc={tc_names} head={head}")
        print(f"    model={req_model} ({api_config['name']}) stream={is_stream} msgs={len(messages)}")

        upstream_url = api_config["url"]
        # 确保 model 名正确
        request_body["model"] = api_config["model"]
        # 强制非流式: 让 proxy 能做策略检查 (不管客户端是否请求 stream)
        request_body["stream"] = False
        # stream_options 只在流式下合法, 上游会拒绝非流式请求携带它
        request_body.pop("stream_options", None)
        body = json.dumps(request_body).encode()
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

        # 非流式: 跑策略检查
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
    # Judge 层凭证检查: 缺凭证时评审层按封闭失败模式全部阻断,
    # 必须给启动者明确信号, 避免误以为防护在工作
    from system.policies.llm_injection_judge import _get_api_credentials
    jm, ju, jk = _get_api_credentials()
    if not all([jm, ju, jk]):
        print()
        print("   ⚠️  警告: 未找到 Judge 层模型凭证 (STEPFUN_API_KEY 等)")
        print("   ⚠️  评审层将以封闭失败模式运行, 所有高风险工具调用一律阻断")
        print("   ⚠️  正常业务也会受影响, 请在 .env.local 配置凭证后重启")
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
