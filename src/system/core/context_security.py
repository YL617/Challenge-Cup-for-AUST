"""上下文安全核心: 来源标注(#3) + 会话状态与攻击链跟踪(#2)。

设计对应架构审查的三条修正:
  1. 污点从"布尔判决"改为"证据": 每段不可信内容打来源标签, 命中标记只作为
     抬档证据, 不再单独决定拦截; 且只看近 N 条消息(衰减), 一处命中不再
     污染整条会话
  2. 会话级状态: 按 session_id 累积侦察事件/敏感路径/暂存动作, 识别
     侦察→归集→外传/破坏 的链条阶段 (D2 "异常任务链阻断"的实现基础)
  3. 阈值升级: 短窗口内多次敏感侦察, L1 自动抬 L2; 判定理由携带链条证据

session_id 推导: OpenAI 协议每轮重放全量历史, 取首条用户消息的哈希前缀,
同一会话各轮稳定; 不同会话首消息不同即分离。
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 敏感路径特征 (侦察/归集阶段判断用)
SENSITIVE_PATH_RE = re.compile(
    r"/(data|registers|archive|official|policy|finance|audit|salary|secrecy)"
    r"|台账|名册|薪资|合同|审计|涉密|预算|报销|信访"
    # 定向检索特定部门的文件同样属于敏感侦察 (v11c-45 实证: find *财务科* 未计入)
    r"|财务科|人事科|档案室|信息科|办公室|会务科|资产科|法规科|信访办|机关党委",
)

# 外传/破坏即危害阶段 (与 intent_classifier 的口径保持一致)
HARM_RE = re.compile(
    r"(curl|wget|scp|rsync|nc)\s+[^|]*(-T|-F|--data|-X\s*POST|@|//|:)"
    r"|rm\s+(-[rf]|[^-])|shred|truncate\s|sed\s+-i|DELETE\s+FROM|vacuum",
    re.IGNORECASE,
)

# 归集动作 (侦察与危害之间的中间段: 打包/拷贝/暂存)
STAGING_RE = re.compile(
    r"(tar\s|zip\s|cp\s|mv\s|>>)|打包|压缩|拷贝|汇总",
    re.IGNORECASE,
)

RECON_RE = re.compile(
    r"(find\s|ls\s|du\s|df\s|stat\s|grep|show\s+tables|describe|locate)",
    re.IGNORECASE,
)


def derive_session_id(messages: List[Dict]) -> str:
    """首条用户消息哈希前缀作为会话号 (跨轮稳定)。"""
    for m in messages or []:
        if m.get("role") != "user":
            continue
        c = m.get("content", "")
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
        return "s-" + hashlib.sha1(str(c)[:300].encode("utf-8")).hexdigest()[:12]
    return "s-anon"


def msg_texts(m: Dict) -> List[str]:
    c = m.get("content", "")
    if isinstance(c, str):
        return [c] if c.strip() else []
    if isinstance(c, list):
        return [b.get("text", "") for b in c if isinstance(b, dict) and b.get("text")]
    return []


@dataclass
class SourceEvidence:
    """一段不可信内容中的标记命中证据。"""
    source_type: str          # user_input / tool_result
    role: str
    head: str
    markers: List[str]
    msg_index: int


@dataclass
class ChainEvent:
    ts: float
    stage: str                # recon / staging / harm
    tool: str
    detail: str


@dataclass
class SessionState:
    session_id: str
    created_ts: float = field(default_factory=time.time)
    recon_events: List[ChainEvent] = field(default_factory=list)
    staging_events: List[ChainEvent] = field(default_factory=list)
    harm_events: List[ChainEvent] = field(default_factory=list)
    sensitive_touched: List[str] = field(default_factory=list)
    decisions: List[Dict] = field(default_factory=list)
    input_risk: int = 0       # 输入侧累计风险 (#4 输入扫描层写入)

    def chain_stage(self) -> str:
        if self.harm_events:
            return "harm"
        if self.staging_events:
            return "staging"
        if self.recon_events:
            return "recon"
        return "idle"

    def chain_evidence(self) -> Dict:
        """给判定理由携带的链条证据摘要。"""
        return {
            "stage": self.chain_stage(),
            "recon_count": len(self.recon_events),
            "staging_count": len(self.staging_events),
            "sensitive_touched": self.sensitive_touched[:5],
            "recent_recon": [e.detail[:60] for e in self.recon_events[-3:]],
        }


def _config_get(dotted, default):
    """配置读取 (缺失/异常时回退默认, 不影响主流程)。"""
    try:
        from system.core.policy_config import get_config
        v = get_config().get(dotted, default)
        return v if v is not None else default
    except Exception:
        return default


def _sensitive_re():
    """敏感侦察词表自配置编译 (缓存于模块级, 配置热加载后自动更新)。"""
    global _SENSITIVE_RE_CACHE, _SENSITIVE_RE_KEY
    pats = _config_get("recon.sensitive_patterns", None) or []
    key = str(pats)
    if _SENSITIVE_RE_CACHE is None or _SENSITIVE_RE_KEY != key:
        _SENSITIVE_RE_CACHE = re.compile("|".join(re.escape(p) for p in pats)) if pats else SENSITIVE_PATH_RE
        _SENSITIVE_RE_KEY = key
    return _SENSITIVE_RE_CACHE


_SENSITIVE_RE_CACHE = None
_SENSITIVE_RE_KEY = ""


class ContextSecurity:
    """请求级来源标注 + 会话级状态管理 (线程安全, 进程内单例)。"""

    # 标记命中只看近 N 条消息: 单点命中不再污染整条会话 (#3)
    TAINT_WINDOW = 8
    # 升级阈值: 时效窗口内敏感侦察达到该次数, 后续侦察 L1→L2 (#2)
    RECON_ESCALATE_N = 3
    RECON_WINDOW_S = 1200

    @property
    def taint_window(self) -> int:
        return int(_config_get("thresholds.taint_window", self.TAINT_WINDOW))

    @property
    def recon_escalate_n(self) -> int:
        return int(_config_get("thresholds.recon_escalate_n", self.RECON_ESCALATE_N))

    @property
    def recon_window_s(self) -> int:
        return int(_config_get("thresholds.recon_window_s", self.RECON_WINDOW_S))

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, SessionState] = {}
        # tool_call_id -> path: 技能库读取(可信来源)的调用图判定结果
        self._trusted_call_ids: Dict[str, str] = {}

    # ---------------- 请求侧: 来源标注与证据收集 (#3) ----------------

    def tag_sources(
        self,
        messages: List[Dict],
        markers: List[str],
        trusted_tool_call_ids: Optional[Dict[str, str]] = None,
        trust_content_fn=None,
    ) -> Tuple[List[SourceEvidence], Dict[str, List[str]]]:
        """近窗口内不可信消息的标记扫描 → (证据列表, 按来源分组命中)。

        信任模型: system/assistant 可信; tool-result 技能读取可信(调用图
        tool_call_id 判定, 或调用图缺失时按 trust_content_fn 内容指纹兜底);
        其余 user/tool 消息为不可信来源。
        """
        evidences: List[SourceEvidence] = []
        if trusted_tool_call_ids:
            with self._lock:
                self._trusted_call_ids.update(trusted_tool_call_ids)
        window = messages[-self.taint_window:]
        offset = len(messages) - len(window)
        for i, m in enumerate(window):
            role = m.get("role", "")
            if role in ("system", "assistant"):
                continue
            texts = msg_texts(m)
            if role == "tool":
                if m.get("tool_call_id") in self._trusted_call_ids:
                    continue
                if trust_content_fn and all(trust_content_fn(t) for t in texts if t):
                    continue  # 技能正文指纹兜底
            joined = "\n".join(texts)
            if not joined.strip():
                continue
            low = joined.lower()
            hit = [mk for mk in markers if mk in low]
            if hit:
                evidences.append(SourceEvidence(
                    source_type="user_input" if role == "user" else "tool_result",
                    role=role, head=joined[:100], markers=hit[:4],
                    msg_index=offset + i,
                ))
        grouped: Dict[str, List[str]] = defaultdict(list)
        for e in evidences:
            grouped[e.source_type].extend(e.markers)
        return evidences, grouped

    # ---------------- 会话状态与攻击链 (#2) ----------------

    def state(self, session_id: str) -> SessionState:
        with self._lock:
            if session_id not in self._sessions:
                self._sessions[session_id] = SessionState(session_id=session_id)
            return self._sessions[session_id]

    def record_chain_event(self, session_id: str, tool: str, op_text: str) -> Optional[str]:
        """记录一次放行/执行的调用到链条, 返回事件阶段 (不适合归链返回 None)。"""
        st = self.state(session_id)
        low = (op_text or "").lower()
        stage = None
        if HARM_RE.search(low):
            stage = "harm"
        elif STAGING_RE.search(low):
            stage = "staging"
        elif RECON_RE.search(low):
            stage = "recon"
        if not stage:
            return None
        ev = ChainEvent(ts=time.time(), stage=stage, tool=tool,
                        detail=(op_text or "")[:120])
        with self._lock:
            {"recon": st.recon_events, "staging": st.staging_events,
             "harm": st.harm_events}[stage].append(ev)
            # 事件量上限, 防长会话膨胀
            if len(st.recon_events) > 60:
                st.recon_events = st.recon_events[-30:]
            if _sensitive_re().search(op_text or ""):
                st.sensitive_touched.append((op_text or "")[:60])
                if len(st.sensitive_touched) > 30:
                    st.sensitive_touched = st.sensitive_touched[-15:]
        return stage

    def recon_pressure(self, session_id: str) -> int:
        """时效窗口内的敏感侦察次数 (升级判定)。"""
        st = self.state(session_id)
        now = time.time()
        return sum(
            1 for e in st.recon_events
            if now - e.ts <= self.recon_window_s
            and _sensitive_re().search(e.detail)
        )

    def should_escalate_recon(self, session_id: str) -> Tuple[bool, Dict]:
        """侦察压力达阈值 → 建议 L1 抬 L2, 附链条证据。"""
        pressure = self.recon_pressure(session_id)
        return pressure >= self.recon_escalate_n, {
            "recon_pressure": pressure,
            **self.state(session_id).chain_evidence(),
        }

    def record_decision(self, session_id: str, decision: Dict) -> None:
        st = self.state(session_id)
        with self._lock:
            st.decisions.append(decision)
            if len(st.decisions) > 200:
                st.decisions = st.decisions[-100:]

    def add_input_risk(self, session_id: str, n: int) -> None:
        with self._lock:
            self.state(session_id).input_risk += n
