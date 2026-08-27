"""审计链 (#5): 哈希链防篡改写入 + 单条完整决策路径 + 会话可串联。

修正的问题: 原审计条目无 session/trace 标识, 一次判定的路径(意图/规则/污点/
judge/处置)散在多条记录里拼不回去, 并发场景靠行数偏移对齐会串。

方案:
  - 每条审计带 session_id / round / ts
  - 工具调用判定落一条 type="decision" 的完整路径记录
  - 全部条目串哈希链 (prev_hash → hash), 改任何一条都会断链
  - export_trace.py 按 session 导出证据包并验链
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional


class AuditChain:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else Path("src/system/proxy/audit_log.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash = self._load_last_hash()

    def _load_last_hash(self) -> str:
        if not self.path.exists():
            return "GENESIS"
        last = ""
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = line
        if not last:
            return "GENESIS"
        try:
            return json.loads(last).get("hash", "GENESIS")
        except json.JSONDecodeError:
            return "GENESIS"

    @staticmethod
    def _canonical(entry: Dict) -> str:
        return json.dumps(entry, ensure_ascii=False, sort_keys=True)

    def append(self, entry: Dict) -> Dict:
        """补全链字段并落盘, 返回最终条目。"""
        with self._lock:
            entry = dict(entry)
            entry.setdefault("ts", time.time())
            entry["prev_hash"] = self._last_hash
            entry["hash"] = hashlib.sha256(
                self._canonical(entry).encode("utf-8")
            ).hexdigest()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._last_hash = entry["hash"]
            return entry

    def entries(self) -> List[Dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return out

    def by_session(self, session_id: str) -> List[Dict]:
        return [e for e in self.entries() if e.get("session_id") == session_id]

    def verify_chain(self) -> List[int]:
        """返回断链位置索引 (空=完整)。

        哈希链从引入起的条目开始计算; 旧版无 hash 字段的遗留条目跳过
        (链完整性只对链接后的条目有意义)。
        """
        broken = []
        prev = "GENESIS"
        for i, e in enumerate(self.entries()):
            if "hash" not in e or "prev_hash" not in e:
                continue  # 升级前的遗留条目
            if e["prev_hash"] != prev:
                broken.append(i)
            body = {k: v for k, v in e.items() if k != "hash"}
            expect = hashlib.sha256(self._canonical(body).encode("utf-8")).hexdigest()
            if e["hash"] != expect:
                broken.append(i)
            prev = e["hash"]
        return broken
