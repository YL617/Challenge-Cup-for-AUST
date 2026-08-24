#!/usr/bin/env python3
"""审计证据包导出 (#5, D4)。

用法:
  python3 src/scripts/export_trace.py --session s-abc123def456
  python3 src/scripts/export_trace.py --session s-abc123def456 --out bundle.json
  python3 src/scripts/export_trace.py --verify        # 只验哈希链

输出: 该会话的全部审计条目(按时间) + 链完整性校验 + 人读摘要。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from system.core.audit_chain import AuditChain

AUDIT = AuditChain()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="", help="会话号 (audit 条目的 session_id)")
    ap.add_argument("--confirm", default="", help="按审批号过滤")
    ap.add_argument("--out", default="", help="输出 json 路径 (默认打印)")
    ap.add_argument("--verify", action="store_true", help="验整条哈希链")
    args = ap.parse_args()

    if args.verify:
        broken = AUDIT.verify_chain()
        n = len(AUDIT.entries())
        if broken:
            print(f"❌ 链在 {len(broken)} 处断裂: 索引 {broken[:10]} (共 {n} 条)")
            sys.exit(1)
        print(f"✅ 哈希链完整 ({n} 条, 无篡改)")
        return

    entries = AUDIT.entries()
    if args.session:
        entries = [e for e in entries if e.get("session_id") == args.session]
    if args.confirm:
        entries = [e for e in entries if e.get("confirm_id") == args.confirm]
    if not entries:
        print("无匹配条目")
        sys.exit(1)

    decisions = [e for e in entries if e.get("type") == "decision"]
    bundle = {
        "session": args.session or "(all)",
        "n_entries": len(entries),
        "chain_ok": not AUDIT.verify_chain(),
        "decisions": decisions,
        "all_entries": entries,
    }

    # 人读摘要
    print(f"会话 {args.session or '(all)'}: {len(entries)} 条审计, "
          f"{len(decisions)} 次工具判定\n")
    for d in decisions:
        j = d.get("judge") or "-"
        ch = (d.get("chain") or {})
        print(f"[r{d.get('round','?')}] {d.get('tool','?'):12} "
              f"{d.get('intent','?'):20} → {d.get('disposition','?')}")
        print(f"    规则: {d.get('rule','') or '-'}  judge: {j[:40]}")
        print(f"    链条: stage={ch.get('stage','-')} recon={ch.get('recon_count',0)} "
              f"staging={ch.get('staging_count',0)}")
        if d.get("confirm_id"):
            print(f"    审批: {d['confirm_id']}")
        print(f"    理由: {d.get('reason','')}")
        print()

    if args.out:
        Path(args.out).write_text(
            json.dumps(bundle, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"证据包已写出: {args.out}")


if __name__ == "__main__":
    main()
