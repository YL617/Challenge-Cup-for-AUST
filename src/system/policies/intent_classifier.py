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
  DATA_TAMPERING      — 台账就地批量改写 (Impact·数据完整性)
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
    # 等效删除: 不出现 rm 也能把数据弄没 (2026-08-23 同义改写复核补入)
    "unlink ", "dd if=/dev/zero", "dd if=/dev/random", "mkfs", "> /dev/null 2>&1 &&",
]
# 就地批量改写: 台账/公文被原地覆盖, 原值不可恢复。
# 不归 DESTRUCTIVE(那是删除), 单列一类, 因为"批量改文号年度"这类正当业务
# 与"批量改经办人/金额/办结状态"在命令层面同形, 合法性取决于带外事实 → L2 人工确认。
_TAMPER_RE = re.compile(
    r"sed\s+(-[a-z]*i|--in-place)|perl\s+-[a-z]*i|"
    r"\bawk\b[^|]*\|\s*tee\s|python3?\s+-c[^\n]*write_text|"
    r"\bin-place\b|\bxargs\s+sed\b",
    re.IGNORECASE,
)
# exec 中的数据外发关键词
_EXEC_EXPORT_KW = [
    "curl ", "wget ", "scp ", "ftp ", "nc ", "ncat ", "rsync ",
    # 备用外传通道 (同义改写复核补入): 不用 curl 也能把数据送出去
    "/dev/tcp/", "openssl s_client", "git push", "sendmail", "mailx ",
    "sftp ", "ssh ", "socat ", "telnet ", "nc.traditional",
]
# 外部目的地: 拷贝类命令的落点在这些位置 = 出了内网数据区
_EXTERNAL_DEST_RE = re.compile(
    r"https?://|[\w.-]+@[\w.-]+:|\d{1,3}(\.\d{1,3}){3}:|"
    r"/volumes/|/mnt/|/media/|/srv/outbox|/outbox/|smb://|//[\w.-]+/",
    re.IGNORECASE,
)
_COPY_VERB_RE = re.compile(r"^\s*(cp|mv|rsync|tar\s+[a-z]*c|zip|scp)\b", re.IGNORECASE)
# 只读的系统查询子命令: 不是篡改, 归侦察, 否则 systemctl status 这类日常排查被拦死
_READONLY_SYS_RE = re.compile(
    r"\b(systemctl|journalctl|service)\s+(status|show|list-units|list-unit-files|is-active|is-enabled|cat)\b|"
    r"\bjournalctl\s+(-[a-z]+\s+)*(-n|--lines|-u|--since)\b",
    re.IGNORECASE,
)
# 临时/自有工作区: 在这些路径下删自己的中间产物不是破坏行为
_SCRATCH_PATH_RE = re.compile(r"(^|\s)(/tmp/|/var/tmp/|/private/tmp/|\./tmp/|~/\.cache/)")
# 业务数据区: 判"把数据搬离/清零"时用来认源头
_DATA_ROOT_RE = re.compile(r"/data/|/srv/data|/archive/|台账|档案|ledger|registers", re.IGNORECASE)
# 重定向清零: `: > f` / `> f` / `truncate -s 0` 都能把文件内容抹掉
_TRUNCATE_RE = re.compile(
    r'(^|[;&|]|\bdo\b)\s*:?\s*>\s*["\']?[\w/.$-]|truncate\s+-s\s*0'
)
# which/type 只是探测某命令在不在, 不是真的外传
_PROBE_ONLY_RE = re.compile(r"^\s*(which|type|command\s+-v|hash)\b", re.IGNORECASE)


def _split_segments(op: str) -> list:
    """按 shell 分隔符拆命令段, 但引号内的分隔符不算。

    朴素的 re.split 会把 python3 -c "a;b" 拆成两段, 载荷特征被切碎,
    分类直接失效 —— 这是把 `;` 塞进引号就能绕过整层检测的口子。
    """
    segs, buf, quote = [], [], ""
    i = 0
    while i < len(op):
        ch = op[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in ";|&":
            # 连续的 && / || 一并吃掉
            while i + 1 < len(op) and op[i + 1] in ";|&":
                i += 1
            segs.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [x.strip() for x in segs if x.strip()]


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
# 配置/持久化路径: 往这里写 = 给智能体装长期生效的规则 (v11-79 外发规则落盘实证)
_CONFIG_PATH_KW = [
    "/etc/", "/usr/local/etc/", ".openclaw", ".opencode", ".config/",
    ".bashrc", ".zshrc", ".profile", "crontab", "systemd",
    "settings.json", "config.json", "config.yaml", "config.yml",
    ".conf", ".ini", "mail_rules", "sync_rules", "agents.md", "claude.md",
]
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


def _has_verified_internal_address(recipients_str: str) -> bool:
    """收件人里是否存在可验证的内部地址 (带内部域名)"""
    for token in recipients_str.replace(";", " ").replace(",", " ").split():
        if "@" not in token:
            continue
        domain = token.rsplit("@", 1)[-1].strip(">").strip()
        if domain and domain.endswith(_INTERNAL_DOMAIN_SUFFIXES):
            return True
    return False


# 个人敏感信息(PII): 手机号/身份证号/明确 PII 字段名
_PII_RE = re.compile(
    r"1[3-9]\d{9}|\d{17}[\dXx]|身份证|手机号|手机|id_card|id number|passport",
    re.IGNORECASE,
)


def _classify_exec_segment(seg: str, full_op: str = "") -> str:
    """单段 shell 命令的意图分类（组合命令分段分级用）。

    优先级: SYSTEM_TAMPERING > DESTRUCTIVE > TAMPERING > AUTHORITY > RECON >
            EXPORT > NORMAL
    （段内仍是首个匹配; 跨段严重度合成由 classify_intent 负责）

    full_op 传整条命令: `for f in /data/x/*; do : > "$f"; done` 拆完之后,
    清零动作和它作用的数据路径落在不同段里, 只看段内会两边都判不出来。
    """
    scope = full_op or seg
    # 只读系统查询先摘出去: systemctl status / journalctl -n 是日常排查,
    # 落到 SYSTEM_TAMPERING 会把正常运维拦死 (2026-08-23 良性探针实证)
    if _READONLY_SYS_RE.search(seg):
        return "RECONNAISSANCE"
    if any(kw in seg for kw in _SYSTEM_TAMPERING_KW):
        return "SYSTEM_TAMPERING"
    if any(kw in seg for kw in _DESTRUCTIVE_KW):
        # 删自己在 /tmp 下的中间产物不是破坏行为
        if _SCRATCH_PATH_RE.search(seg) and not _DATA_ROOT_RE.search(seg):
            return "NORMAL_OPERATION"
        return "DESTRUCTIVE_ACTION"
    # 等效删除: 把业务数据清零, 或从数据区搬到临时区 (效果同 rm, 命令里没有 rm)
    if _DATA_ROOT_RE.search(scope):
        if _TRUNCATE_RE.search(seg):
            return "DESTRUCTIVE_ACTION"
        if re.match(r"^\s*mv\b", seg, re.IGNORECASE) and _SCRATCH_PATH_RE.search(seg):
            return "DESTRUCTIVE_ACTION"
        # tee/重定向把管道结果写回原数据文件 = 就地改写
        if re.search(r"\btee\b", seg) and _DATA_ROOT_RE.search(seg):
            return "DATA_TAMPERING"
    if _TAMPER_RE.search(seg) or _TAMPER_RE.search(scope):
        return "DATA_TAMPERING"
    if any(kw in seg for kw in _AUTHORITY_KW):
        return "AUTHORITY_OPERATION"
    # 外传判定必须排在侦察前面。侦察正则会命中 dump/find/ls 这类词,
    # 而外传命令里常常同时带着它们 (openssl ... < dump.json), 顺序反了就会
    # 被侦察掩护掉降到 L1 —— 与 v11-79 那次穿透同一类错误。
    if not _PROBE_ONLY_RE.search(seg):
        # 拷贝/切分类命令按落点判: 目的地在外部就是外传, 不看用了哪个命令。
        # cp -r /data/办公室 /Volumes/vendor_share/ 与 scp 出去等价
        if _EXTERNAL_DEST_RE.search(seg) and (
            _COPY_VERB_RE.search(seg) or re.search(r"\b(split|dd|tee|cat)\b", seg)
        ):
            return "DATA_EXPORT"
        if any(kw in seg for kw in _EXEC_EXPORT_KW):
            return "DATA_EXPORT"
        # 脚本语言拉起的网络外发: python/perl/ruby/node 直接开 socket 或 HTTP
        if re.search(
            r"(python3?|perl|ruby|node|php)\b[^\n]*"
            r"(urllib|requests\.|http\.client|socket|net/http|fetch\(|axios)",
            seg, re.IGNORECASE,
        ):
            return "DATA_EXPORT"
    if _RECONNAISSANCE_RE.search(seg):
        return "RECONNAISSANCE"
    return "NORMAL_OPERATION"


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

    # ---- exec 类：组合命令分段分级 (v11-79 穿透实证) ----
    # 命令链 "find /data -name x | curl --data-binary @- https://..." 整串
    # 匹配会命中 RECON (侦察检查在外传之前) → L1 放行外传。修复: 按分隔符
    # 拆段逐段分类, 取最严重意图 (危害段主导, 侦察段不掩盖外传段)。
    if tool in _EXEC_TOOLS:
        segments = _split_segments(op)
        intents = [_classify_exec_segment(s, op) for s in segments] or ["NORMAL_OPERATION"]
        for severe in ("SYSTEM_TAMPERING", "DESTRUCTIVE_ACTION"):
            if severe in intents:
                return severe
        for mid in ("DATA_EXPORT", "DATA_TAMPERING", "AUTHORITY_OPERATION"):
            if mid in intents:
                return mid
        if "RECONNAISSANCE" in intents:
            return "RECONNAISSANCE"
        return "NORMAL_OPERATION"

    # ---- 邮件类：按收件人 + 会话内频率判定 ----
    if tool in _EMAIL_TOOLS:
        # 外部/群发 → DATA_EXPORT (白名单域名模型, 见 _has_external_recipient)
        if _has_external_recipient(recipients_str):
            return "DATA_EXPORT"
        # PII 外发 + 收件人无可验证内部地址 (占位符如"对接人邮箱"/裸部门名)
        # → 无法确认目的地在境内内部, 送语义层复核 (v9-01 OpenClaw 穿透实证)
        body_str = (canonical.get("content", "") or "") + " " + op
        if _PII_RE.search(body_str) and not _has_verified_internal_address(recipients_str):
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
        # 写配置/规则文件 = 给智能体装长期生效的行为, 属持久化篡改
        if any(kw in path for kw in _CONFIG_PATH_KW):
            return "SYSTEM_TAMPERING"
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
# 处置分层: 意图 → L0 放行 / L1 上报 / L2 确认 / L3 阻断
# L1 (侦察类放行+上报): 危害发生在外传/破坏动作本身, 拦侦察只会把攻击链
#   推到看不见的路径 (v11c-13/19 实证); L1 让链条走到危害动作在 L3 逮住
# L2 (半合法操作): 跨部门外发/审批类操作的合法性取决于带外事实(v9-16 实证),
#   规则和语义层都无法判定, 处置为暂停待人工确认
# L3 (确定性危害): 破坏/篡改/结构规则命中, 直接阻断并返回短原因
DISPOSITION_POLICY = {
    "NORMAL_OPERATION": "L0",
    "RECONNAISSANCE": "L1",
    "DATA_EXPORT": "L2",
    "AUTHORITY_OPERATION": "L2",
    "DESTRUCTIVE_ACTION": "L3",
    "SYSTEM_TAMPERING": "L3",
    # 就地批量改写台账: 与正当的"批量改文号年度"命令同形, 合法性取决于
    # 带外事实(到底该不该改), 规则和语义层都判不了 → 停下来等人工确认
    "DATA_TAMPERING": "L2",
}


def intent_to_disposition(intent: str) -> str:
    """意图类别 → 处置层级 (L0/L1/L2/L3)。外置配置优先 (#7)。"""
    try:
        from system.core.policy_config import get_config
        return get_config().disposition_for(intent)
    except Exception:
        return DISPOSITION_POLICY.get(intent, "L0")
