"""
core/tool_parsers.py — 政务工具解析器注册表

ArbiterOS 用 YAML DSL 引擎（openclaw.yaml 9KB + engine.py 10KB）做工具解析，
我们直接用 Python 函数实现，更轻量、更可读。

覆盖 case 库中所有工具名（block-01 + block-03）：
  高频：read(92) write(21) kb_search(19) send_email(14) exec(10) cron(5)
  中频：calendar_list_events(4) knowledge_base_query(4) calendar_create(2)
        send_email_batch(1) calendar_create_event(1) list_emails(2)
  低频：click_link browser_open browser_upload submit_form send_sms share_file approve
  外部 API（InjecAgent 改写）：GmailReadEmail GmailSendEmail 等 → 归类为 OUTPUT
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from .types import (
    TaintStatus,
    ToolParseResult,
    make_security_type,
)


# ---------------------------------------------------------------------------
# 敏感路径检测（政务专用增强）
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PATTERNS = [
    (re.compile(r"budget", re.I), "HIGH"),
    (re.compile(r"salary", re.I), "HIGH"),
    (re.compile(r"hr[\-_]", re.I), "HIGH"),
    (re.compile(r"personnel", re.I), "HIGH"),
    (re.compile(r"finance", re.I), "HIGH"),
    (re.compile(r"internal[\-_]", re.I), "HIGH"),
    (re.compile(r"confidential", re.I), "HIGH"),
    (re.compile(r"address[\-_]?book", re.I), "HIGH"),
    (re.compile(r"secret", re.I), "HIGH"),
    (re.compile(r"password", re.I), "HIGH"),
    (re.compile(r"api[\-_]?key", re.I), "HIGH"),
    (re.compile(r"/etc/passwd", re.I), "HIGH"),
    (re.compile(r"/etc/shadow", re.I), "HIGH"),
]

_UNTRUSTED_SOURCE_PATTERNS = [
    re.compile(r"phishing", re.I),
    re.compile(r"external", re.I),
    re.compile(r"untrusted", re.I),
    re.compile(r"attacker", re.I),
]


def _classify_path_confidentiality(path: str) -> str:
    """根据路径判断机密度。返回 LOW / HIGH。"""
    for pattern, level in _SENSITIVE_PATH_PATTERNS:
        if pattern.search(path):
            return level
    return "LOW"


def _is_untrusted_source(path_or_name: str) -> bool:
    """判断路径或来源是否为不可信。"""
    return any(p.search(path_or_name) for p in _UNTRUSTED_SOURCE_PATTERNS)


# ---------------------------------------------------------------------------
# 工具解析器
# ---------------------------------------------------------------------------


def _parse_read(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """read / read_file / list_emails / GmailReadEmail → READ"""
    path = str(args.get("path") or args.get("file_path") or args.get("folder") or "")
    conf = _classify_path_confidentiality(path)
    trust = "LOW" if _is_untrusted_source(path) else "HIGH"
    return ToolParseResult(
        instruction_type="READ",
        security_type=make_security_type(
            confidentiality=conf,
            trustworthiness=trust,
            confidence="UNKNOWN",
            reversible=True,
            authority="UNKNOWN",
            risk="HIGH" if conf == "HIGH" else "LOW",
        ),
    )


def _parse_write(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """write / save → WRITE"""
    path = str(args.get("path") or args.get("file_path") or "")
    conf = _classify_path_confidentiality(path)
    is_policy = "policy" in path.lower()
    return ToolParseResult(
        instruction_type="WRITE",
        security_type=make_security_type(
            confidentiality=conf,
            trustworthiness="HIGH",
            confidence="UNKNOWN",
            reversible=not is_policy,  # 政策文件修改不可逆
            authority="UNKNOWN",
            risk="HIGH" if (conf == "HIGH" or is_policy) else "MID",
        ),
    )


def _parse_exec(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """exec / TerminalExecute → EXEC（默认高风险）"""
    cmd = str(args.get("command") or "")
    is_dangerous = any(
        kw in cmd.lower()
        for kw in ["rm ", "rm -", "delete", "curl ", "wget ", "| sh", "fork"]
    )
    return ToolParseResult(
        instruction_type="EXEC",
        security_type=make_security_type(
            confidentiality="UNKNOWN",
            trustworthiness="UNKNOWN",
            confidence="UNKNOWN",
            reversible=not is_dangerous,
            authority="UNKNOWN",
            risk="HIGH",
        ),
    )


def _parse_send_email(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """send_email / send_email_batch / GmailSendEmail / send_sms → 外发操作"""
    to = str(args.get("to") or args.get("recipient") or "")
    is_external = "example.com" in to or "attacker" in to.lower()
    return ToolParseResult(
        instruction_type="WRITE",  # 外发视为写操作（产生副作用）
        security_type=make_security_type(
            confidentiality="HIGH" if is_external else "LOW",
            trustworthiness="HIGH",
            confidence="UNKNOWN",
            reversible=False,  # 发出去的邮件收不回
            authority="UNKNOWN",
            risk="HIGH" if is_external else "MID",
        ),
    )


def _parse_kb_search(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """kb_search / knowledge_base_query → RETRIEVE（不可信来源）"""
    return ToolParseResult(
        instruction_type="RETRIEVE",
        security_type=make_security_type(
            confidentiality="LOW",
            trustworthiness="LOW",  # 知识库结果默认不可信
            confidence="UNKNOWN",
            reversible=True,
            authority="UNKNOWN",
            risk="LOW",
        ),
    )


def _parse_calendar(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """calendar_list_events / calendar_create / calendar_create_event / GoogleCalendar*"""
    is_create = "create" in str(args).lower()
    return ToolParseResult(
        instruction_type="WRITE" if is_create else "READ",
        security_type=make_security_type(
            confidentiality="LOW",
            trustworthiness="HIGH",
            confidence="UNKNOWN",
            reversible=is_create,
            authority="UNKNOWN",
            risk="MID" if is_create else "LOW",
        ),
    )


def _parse_cron(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """cron / automation → 定时任务"""
    return ToolParseResult(
        instruction_type="WRITE",
        security_type=make_security_type(
            confidentiality="UNKNOWN",
            trustworthiness="UNKNOWN",
            confidence="UNKNOWN",
            reversible=False,
            authority="UNKNOWN",
            risk="HIGH",
        ),
    )


def _parse_browser(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """browser_open / browser_upload / WebBrowserNavigateTo / click_link / submit_form"""
    action = str(args.get("action") or args.get("url") or "")
    is_output = "upload" in str(args).lower() or "submit" in str(args).lower()
    return ToolParseResult(
        instruction_type="WRITE" if is_output else "READ",
        security_type=make_security_type(
            confidentiality="LOW",
            trustworthiness="LOW",  # 浏览器内容默认不可信
            confidence="UNKNOWN",
            reversible=not is_output,
            authority="UNKNOWN",
            risk="MID" if is_output else "LOW",
        ),
    )


def _parse_approve(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """approve → 高权限操作"""
    return ToolParseResult(
        instruction_type="WRITE",
        security_type=make_security_type(
            confidentiality="UNKNOWN",
            trustworthiness="HIGH",
            confidence="UNKNOWN",
            reversible=False,
            authority="POLICY_APPROVED",
            risk="HIGH",
        ),
    )


def _parse_share_file(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """share_file → 文件共享（外发风险）"""
    return ToolParseResult(
        instruction_type="WRITE",
        security_type=make_security_type(
            confidentiality="HIGH",
            trustworthiness="HIGH",
            confidence="UNKNOWN",
            reversible=False,
            authority="UNKNOWN",
            risk="HIGH",
        ),
    )


def _parse_generic_output(args: Dict[str, Any], taint: Optional[TaintStatus]) -> ToolParseResult:
    """未识别的外部 API（InjecAgent 改写的 Gmail/Evernote/GitHub/Twilio 等）→ 默认 OUTPUT"""
    return ToolParseResult(
        instruction_type="EXEC",
        security_type=make_security_type(
            confidentiality="UNKNOWN",
            trustworthiness="UNKNOWN",
            confidence="UNKNOWN",
            reversible=False,
            authority="UNKNOWN",
            risk="HIGH",
        ),
    )


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

# 工具名 → 解析器的映射
# 使用前缀匹配，按最长前缀优先
_REGISTRY: Dict[str, Any] = {
    # READ 类
    "read": _parse_read,
    "read_file": _parse_read,
    "list_emails": _parse_read,
    "gmailreademail": _parse_read,
    "twiliogetreceivedsms": _parse_read,
    "todoistsearch": _parse_read,
    "evernotemanagersearch": _parse_read,
    # WRITE 类
    "write": _parse_write,
    "save": _parse_write,
    "edit": _parse_write,
    "file_write": _parse_write,
    # EXEC 类
    "exec": _parse_exec,
    "terminalexecute": _parse_exec,
    # 外发类
    "send_email": _parse_send_email,
    "send_email_batch": _parse_send_email,
    "gmailsendemail": _parse_send_email,
    "send_sms": _parse_send_email,
    # 知识库
    "kb_search": _parse_kb_search,
    "knowledge_base_query": _parse_kb_search,
    # 日程
    "calendar_list_events": _parse_calendar,
    "calendar_create": _parse_calendar,
    "calendar_create_event": _parse_calendar,
    "googlecalendar": _parse_calendar,
    # 自动化
    "cron": _parse_cron,
    # 浏览器
    "browser_open": _parse_browser,
    "browser_upload": _parse_browser,
    "webbrowsernavigate": _parse_browser,
    "click_link": _parse_browser,
    "submit_form": _parse_browser,
    # 审批
    "approve": _parse_approve,
    # 共享
    "share_file": _parse_share_file,
}

# 前缀匹配（Gmail/Evernote/GitHub/August/Amazon 等外部 API）
_OUTPUT_PREFIXES = (
    "gmail", "evernote", "github", "august", "amazon",
    "fedex", "epic", "norton", "23andme", "bank",
    "cisco", "emergency", "smartoffice", "govsocial",
    "govcollab", "govcloud", "govfinance", "govemail",
    "govaddress", "govpayment", "networkpolicy",
)


def parse_tool_instruction(
    tool_name: str,
    arguments: Dict[str, Any] | None = None,
    taint_status: Optional[TaintStatus] = None,
) -> ToolParseResult:
    """解析工具调用，返回指令类型 + 安全属性。

    与 ArbiterOS parse_tool_instruction 接口完全一致。
    """
    name = (tool_name or "").strip().lower()
    args = arguments if isinstance(arguments, dict) else {}

    # 精确匹配
    parser = _REGISTRY.get(name)
    if parser:
        return parser(args, taint_status)

    # 前缀匹配（外部 API）
    for prefix in _OUTPUT_PREFIXES:
        if name.startswith(prefix):
            return _parse_generic_output(args, taint_status)

    # 未知工具 → 默认 EXEC（保守策略）
    return ToolParseResult(
        instruction_type="EXEC",
        security_type=make_security_type(
            confidentiality="UNKNOWN",
            trustworthiness="UNKNOWN",
            confidence="UNKNOWN",
            reversible=False,
            authority="UNKNOWN",
            risk="HIGH",
        ),
    )
