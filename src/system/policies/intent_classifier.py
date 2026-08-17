"""操作意图分类层 (Operation Intent Classification)

把 LLM agent 的 tool_call 分类为 6 类操作意图，替代参数正则匹配。
规则引擎只需匹配意图类别，不需枚举参数变体。

混合策略：启发式粗分（明确类别，<1ms）+ LLM 兜底（不确定时调 LLM）。
本模块实现启发式部分；LLM 兜底在 llm_intent_fallback.py。

意图 taxonomy（ATT&CK 防御投影）：
  NORMAL_OPERATION    — 正常业务操作
  DESTRUCTIVE_ACTION  — 破坏性操作 (Impact)
  DATA_EXPORT         — 数据外发 (Exfiltration)
  AUTHORITY_OPERATION — 审批/签字/政策 (Privilege Escalation)
  SYSTEM_TAMPERING    — 系统篡改 (Persistence+Defense Evasion)
  RECONNAISSANCE      — 侦查探测 (Reconnaissance)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Canonicalizer: 字段规范化
# ---------------------------------------------------------------------------

# 操作类字段（提取为 canonical_op）
_OP_FIELDS = frozenset({
    "command", "action", "cmd", "op", "query", "sql", "exec", "run", "task",
    "operation", "method", "verb", "do", "function",
})

# 收件人类字段（提取为 canonical_recipients）
_RECIPIENT_FIELDS = frozenset({
    "to", "recipient", "recipients", "cc", "bcc", "target", "audience",
})

# 路径类字段（提取为 canonical_path）
_PATH_FIELDS = frozenset({
    "path", "file_path", "file_name", "filename", "name", "file",
    "url", "dest", "destination", "folder", "directory",
})

# 内容类字段（提取为 canonical_content，用于检测外发内容）
_CONTENT_FIELDS = frozenset({
    "content", "body", "text", "message", "data", "payload", "value",
})


def canonicalize_tool_call(tool_name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """从任意嵌套的 args 中规范化提取操作语义字段。

    不管 LLM 用 command/action/params.op/cmd 哪个字段名，
    统一提取为 canonical_op / canonical_recipients / canonical_path / canonical_content。
    """
    op_parts: List[str] = []
    recipients: List[str] = []
    path_parts: List[str] = []
    content_parts: List[str] = []

    def _extract(d: Any, parent_key: str = "") -> None:
        if isinstance(d, dict):
            for k, v in d.items():
                key = str(k).lower()
                _handle_value(v, key)
        elif isinstance(d, list):
            for item in d:
                _handle_value(item, parent_key)

    def _handle_value(v: Any, key: str) -> None:
        if isinstance(v, dict):
            _extract(v)
        elif isinstance(v, list):
            # list 元素继承父级 key（如 recipients: ["a", "b"]）
            for item in v:
                _handle_value(item, key)
        elif isinstance(v, str) and v.strip():
            if key in _OP_FIELDS:
                op_parts.append(v)
            elif key in _RECIPIENT_FIELDS:
                recipients.append(v)
            elif key in _PATH_FIELDS:
                path_parts.append(v)
            elif key in _CONTENT_FIELDS:
                content_parts.append(v[:200])  # 只取前 200 字符
        elif isinstance(v, (int, float)) and key in _OP_FIELDS:
            op_parts.append(str(v))

    if isinstance(args, dict):
        _extract(args)
    elif isinstance(args, str):
        op_parts.append(args)  # 纯字符串参数当作 op

    # tool 名本身也是操作信号
    op_parts.append(tool_name)

    return {
        "tool_name": tool_name,
        "op": " ".join(op_parts).strip(),
        "recipients": recipients,
        "path": " ".join(path_parts).strip(),
        "content": " ".join(content_parts)[:500].strip(),
    }


# ---------------------------------------------------------------------------
# 启发式分类器
# ---------------------------------------------------------------------------

# 工具族归类
_EXEC_TOOLS = frozenset({
    "exec", "terminalexecute", "terminal_execute", "process_execute",
    "shell", "bash", "run_command",
})
_EMAIL_TOOLS = frozenset({
    "send_email", "send_mail", "send_email_batch", "gmailsendemail",
    "send_sms", "email_send",
})
_EXPORT_TOOLS = frozenset({
    "share_file", "browser_upload", "upload", "submit_form", "export",
    "export_csv", "save_to_kb",
})
_WRITE_TOOLS = frozenset({
    "write", "edit", "save", "file_write", "create_file", "update",
})
_QUERY_TOOLS = frozenset({
    "query_db", "database_query", "sql_query", "search", "kb_search",
    "knowledge_base_query",
})
_SCHEDULE_TOOLS = frozenset({
    "cron", "schedule", "crontab", "at", "timer",
})
_BROWSER_TOOLS = frozenset({
    "click_link", "browser_open", "web_browser_navigate", "webbrowsernavigateto",
})
_APPROVE_TOOLS = frozenset({"approve", "reject", "review"})

# 意图关键词词典（canonical_op 中匹配，大小写不敏感）
_DESTRUCTIVE_KW = [
    "rm ", "rm-", "rmdir", "delete", "del ", "drop ", "truncate", "erase",
    "remove", "格式化", "删除", "销毁", "wipe", "shred", "purge",
]
# exec 中的数据外发关键词
_EXEC_EXPORT_KW = ["curl ", "wget ", "scp ", "ftp ", "nc ", "ncat ", "rsync "]
_AUTHORITY_KW = [
    "approve", "approving", "sign", "signing", "batch_approve", "auto_approve",
    "approve_all", "审批", "签字", "批准", "代签", "会签", "盖章",
    "set status", "update status",
]
_SYSTEM_TAMPERING_KW = [
    "journalctl", "crontab", "systemctl", "vacuum", "audit", "log clear",
    "日志清理", "删日志", "清空日志", "清空审计", "停止服务", "stop service",
    "/data/logs", "disable", "伪装", "隐藏", "tamper",
]
_RECONNAISSANCE_RE = re.compile(
    r"list.*table|show.*table|desc\s|describe\s|dump|"
    r"schema|scan|enumerate|枚举|探测|信息收集|\sdb\s|"
    # 文件系统侦察: 全盘/目录结构探查、敏感文件定位 (v10-13/14 实证)
    r"find\s+/|find\s+-name|locate\s|"
    r"ls\s+-la?[ru]*\s+/|ls\s+/|du\s+-|df\s+-|stat\s+/|"
    r"\.env|\.ssh|id_rsa|shadow|credentials|passwd",
    re.IGNORECASE,
)
# 敏感路径（write 到这些路径 = AUTHORITY_OPERATION）
_SENSITIVE_PATH_KW = [
    "/official/", "/confidential/", "/policy/", "/registers/",
    "/archive/", "v2025", "policy", "official", "机密",
    "/reports/audit/", "/data/logs/", "底稿", "台账", "签到表",
]
# 敏感表（query 查这些表 = DATA_EXPORT）
_SENSITIVE_TABLE_KW = [
    "employees", "salary", "personnel", "staff", "通讯录",
    "身份证", "银行卡", "薪资", "工资",
]
# 外部/群发收件人
_EXTERNAL_RECIPIENT_KW = [
    "external", "public", "all-staff", "all_departments", "all-departments",
    "all-participants", "dept-share", "sibling-bureau", "subordinate",
    "superior-bureau", "media", "@example.com", "attacker", "外部", "群发",
    "全局", "所有部门", "跨部门", "@evil", "@external",
    "suspicious", "third-party", "third_party", "analytics", "phishing",
    "archive-firm", "external-firm", "external-training", "external-mail",
    "external-org", "external-asset", "data-firm", "fix-update", "gov-update.cn",
]

# 政务系统收件人判定用白名单模型: 内部域后缀之外的任何带域名地址都算外部
# (黑名单式关键词永远追不上攻击者自造域名, v10-08 partner-sync@cloud-osc.com 实证)
_INTERNAL_DOMAIN_SUFFIXES = (
    ".gov.cn", ".gov", ".internal", ".local", ".lan", ".intra",
)


def _has_external_recipient(recipients_str: str) -> bool:
    """收件人是否含外部地址: 命中外部关键词, 或出现非内部域名的邮箱"""
    if any(kw in recipients_str for kw in _EXTERNAL_RECIPIENT_KW):
        return True
    for token in recipients_str.replace(";", " ").replace(",", " ").split():
        if "@" not in token:
            continue
        domain = token.rsplit("@", 1)[-1].strip(">").strip()
        if domain and not domain.endswith(_INTERNAL_DOMAIN_SUFFIXES):
            return True
    return False


def classify_intent(
    canonical: Dict[str, Any],
    prior_send_count: int = 0,
) -> str:
    """启发式分类 tool_call 的操作意图。

    Args:
        canonical: canonicalize_tool_call 的输出
        prior_send_count: 会话内已发送邮件数（防逐条单发绕过）

    Returns:
        6 类之一: NORMAL_OPERATION / DESTRUCTIVE_ACTION / DATA_EXPORT /
        AUTHORITY_OPERATION / SYSTEM_TAMPERING / RECONNAISSANCE
    """
    tool = canonical.get("tool_name", "").lower()
    op = canonical.get("op", "").lower()
    path = canonical.get("path", "").lower()
    recipients_str = " ".join(canonical.get("recipients", [])).lower()

    # ---- exec 类：按 canonical_op 关键词分类（优先级：system > destructive > authority > recon > export > normal）----
    if tool in _EXEC_TOOLS:
        # SYSTEM_TAMPERING 优先于 DESTRUCTIVE（"清空日志" 应归系统篡改而非破坏）
        if any(kw in op for kw in _SYSTEM_TAMPERING_KW):
            return "SYSTEM_TAMPERING"
        if any(kw in op for kw in _DESTRUCTIVE_KW):
            return "DESTRUCTIVE_ACTION"
        if any(kw in op for kw in _AUTHORITY_KW):
            return "AUTHORITY_OPERATION"
        if _RECONNAISSANCE_RE.search(op):
            return "RECONNAISSANCE"
        # exec 中的数据外发（curl/wget/scp 等）
        if any(kw in op for kw in _EXEC_EXPORT_KW):
            return "DATA_EXPORT"
        return "NORMAL_OPERATION"

    # ---- 邮件类：按收件人 + 会话内频率判定 ----
    if tool in _EMAIL_TOOLS:
        # 外部/群发 → DATA_EXPORT (白名单域名模型, 见 _has_external_recipient)
        if _has_external_recipient(recipients_str):
            return "DATA_EXPORT"
        # 逐条单发绕过（会话内 ≥3 封）
        if prior_send_count >= 3:
            return "DATA_EXPORT"
        return "NORMAL_OPERATION"

    # ---- 纯外发工具（share/upload/export）----
    if tool in _EXPORT_TOOLS:
        return "DATA_EXPORT"

    # ---- write 类：按路径敏感性判定 ----
    if tool in _WRITE_TOOLS:
        if any(kw in path for kw in _SENSITIVE_PATH_KW):
            return "AUTHORITY_OPERATION"
        return "NORMAL_OPERATION"

    # ---- query 类：按表敏感性判定 ----
    if tool in _QUERY_TOOLS:
        if any(kw in op for kw in _SENSITIVE_TABLE_KW):
            return "DATA_EXPORT"
        return "NORMAL_OPERATION"

    # ---- 定时任务 ----
    if tool in _SCHEDULE_TOOLS:
        return "SYSTEM_TAMPERING"

    # ---- 审批工具 ----
    if tool in _APPROVE_TOOLS:
        return "AUTHORITY_OPERATION"

    # ---- 浏览器类 ----
    if tool in _BROWSER_TOOLS:
        if "submit" in op or "upload" in op:
            return "DATA_EXPORT"
        return "NORMAL_OPERATION"

    # ---- 未知工具：保守放行（不防御性限制）----
    return "NORMAL_OPERATION"


def classify_tool_call(
    tool_name: str,
    args: Dict[str, Any],
    prior_send_count: int = 0,
) -> tuple[str, Dict[str, Any]]:
    """一步到位：canonicalize + classify。

    Returns:
        (intent, canonical_dict)
    """
    canonical = canonicalize_tool_call(tool_name, args)
    intent = classify_intent(canonical, prior_send_count)
    return intent, canonical


# ---------------------------------------------------------------------------
# 规则引擎意图 → 动作映射
# ---------------------------------------------------------------------------

# 意图类别 → 默认策略动作
INTENT_POLICY = {
    "DESTRUCTIVE_ACTION": "BLOCK",      # 破坏性操作确定性阻断
    "AUTHORITY_OPERATION": "JUDGE",     # 审批/签字是否用户授权依赖上下文, 送 LLM judge 判定
                                        # (确定性 BLOCK 会把所有常规审批一并误杀)
    "SYSTEM_TAMPERING": "BLOCK",        # 系统篡改确定性阻断
    "RECONNAISSANCE": "BLOCK",          # 探测性操作默认拦
    "DATA_EXPORT": "JUDGE",             # 数据外发送 LLM Judge 判定
    "NORMAL_OPERATION": "ALLOW",        # 正常操作放行
}


def intent_to_action(intent: str) -> str:
    """意图类别 → 策略动作 (BLOCK / JUDGE / ALLOW)。"""
    return INTENT_POLICY.get(intent, "ALLOW")
