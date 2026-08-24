"""L2 人工确认队列 (#1): 待确认存储 + 批准/拒绝 + 重试放行 + 超时默认拒。

流程:
  1. 防护代理对 L2 处置的调用创建确认单 (confirm_id), 拦截并告知智能体
     "已提交人工审批(审批号 CN-xxxx), 批准后可重试"
  2. 管理员经 approve.py 查看/批准/拒绝, 留痕(谁/何时/理由)
  3. 智能体重试同一调用时, 代理按 (session, tool, args 哈希) 匹配确认单:
     已批准且在 TTL 内 → 放行并审计 released_by_approval
     已拒绝或超时(默认拒绝) → L3 阻断并说明

存储: logs/pending_confirmations.jsonl, 逐行 JSON, 状态机 pending→approved/denied/expired。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_TTL_S = 600  # 默认值; 运行时读配置 thresholds.approval_ttl_s (#7)


def _ttl() -> int:
    try:
        from system.core.policy_config import get_config
        return int(get_config().get("thresholds.approval_ttl_s", DEFAULT_TTL_S))
    except Exception:
        return DEFAULT_TTL_S


def args_key(tool: str, args: Dict) -> str:
    canonical = json.dumps({"tool": tool, "args": args}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


class ApprovalQueue:
    def __init__(self, store_path: Optional[Path] = None) -> None:
        self.store = Path(store_path) if store_path else Path("logs/pending_confirmations.jsonl")
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq = int(time.time()) % 100000

    def _load(self) -> List[Dict]:
        if not self.store.exists():
            return []
        out = []
        for l in self.store.read_text(encoding="utf-8").splitlines():
            if l.strip():
                try:
                    out.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
        return out

    def _append(self, record: Dict) -> None:
        with self._lock:
            with open(self.store, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _next_id(self) -> str:
        with self._lock:
            self._seq += 1
            return f"CN-{self._seq:06d}"

    def create(self, session_id: str, tool: str, args: Dict, reason: str,
               ttl_s: Optional[int] = None) -> Dict:
        """创建待确认单, 返回含 confirm_id 的记录。"""
        now = time.time()
        ttl_s = ttl_s if ttl_s is not None else _ttl()
        rec = {
            "confirm_id": self._next_id(),
            "session_id": session_id,
            "tool": tool,
            "args": args,
            "args_key": args_key(tool, args),
            "reason": reason,
            "status": "pending",
            "created_ts": now,
            "expires_ts": now + ttl_s,
            "decided_by": None,
            "decided_ts": None,
            "note": None,
        }
        self._append(rec)
        return rec

    def decide(self, confirm_id: str, approve: bool, by: str, note: str = "") -> Optional[Dict]:
        """批准/拒绝一张确认单 (追加终态行, 原 pending 行保留审计)。"""
        target = None
        for rec in reversed(self._load()):
            if rec.get("confirm_id") == confirm_id and rec.get("status") == "pending":
                target = dict(rec)
                break
        if not target:
            return None
        target.update({
            "status": "approved" if approve else "denied",
            "decided_by": by,
            "decided_ts": time.time(),
            "note": note,
        })
        self._append(target)
        return target

    def check_retry(self, session_id: str, tool: str, args: Dict) -> Optional[Dict]:
        """重试匹配: 返回该确认单最新状态 (denied/expired → 升 L3; pending → 继续拦)。

        每单以最新状态行为准 (decide 追加终态后, 原 pending 行即被取代);
        超时的 pending 即时落一条 expired 终态 (默认拒绝)。
        """
        key = args_key(tool, args)
        latest: Optional[Dict] = None
        for rec in reversed(self._load()):
            if (rec.get("session_id") == session_id
                    and rec.get("args_key") == key):
                latest = rec
                break
        if not latest:
            return None
        if latest.get("status") == "pending":
            if time.time() > latest["expires_ts"]:
                final = dict(latest)
                final.update({"status": "expired", "decided_by": "(timeout)",
                              "decided_ts": time.time(),
                              "note": "TTL 内无人处理, 默认拒绝"})
                self._append(final)
                return final
            return latest
        if latest.get("status") in ("denied", "expired"):
            return latest
        return None

    def consume_if_approved(self, session_id: str, tool: str, args: Dict) -> Optional[Dict]:
        """重试时消费已批准的确认单 (写 released 行, 一次性生效)。"""
        key = args_key(tool, args)
        latest: Optional[Dict] = None
        for rec in reversed(self._load()):
            if (rec.get("session_id") == session_id
                    and rec.get("args_key") == key
                    and rec.get("status") == "approved"):
                latest = rec
                break
        if not latest:
            return None
        final = dict(latest)
        final.update({"status": "released", "decided_ts": time.time(),
                      "note": "重试命中已批准确认单, 已放行"})
        self._append(final)
        return final

    def list_pending(self) -> List[Dict]:
        """当前待处理列表 (每张单取最新状态行)。"""
        latest: Dict[str, Dict] = {}
        for rec in self._load():
            latest[rec["confirm_id"]] = rec
        out = []
        now = time.time()
        for rec in latest.values():
            if rec.get("status") != "pending":
                continue
            out.append({
                "confirm_id": rec["confirm_id"],
                "session": rec["session_id"],
                "tool": rec["tool"],
                "args_head": json.dumps(rec.get("args", {}), ensure_ascii=False)[:100],
                "reason": rec.get("reason", "")[:80],
                "ttl_left_s": max(0, int(rec.get("expires_ts", 0) - now)),
            })
        return sorted(out, key=lambda r: r["confirm_id"])
