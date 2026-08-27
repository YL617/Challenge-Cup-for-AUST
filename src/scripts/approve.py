#!/usr/bin/env python3
"""L2 人工审批 CLI (#1)。

用法:
  python3 src/scripts/approve.py list                    # 待确认队列
  python3 src/scripts/approve.py approve CN-000012 --by 张三 --note 演练已备案
  python3 src/scripts/approve.py deny CN-000012 --by 张三 --note 无此业务
批准后, 智能体重试同一调用即放行 (TTL 内); 拒绝或 10 分钟无人处理默认拒绝。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from system.core.approval_queue import ApprovalQueue

APPROVALS = ApprovalQueue()


def main():
    ap = argparse.ArgumentParser(description="L2 人工审批")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="待确认队列")

    p_ap = sub.add_parser("approve", help="批准")
    p_ap.add_argument("confirm_id")
    p_ap.add_argument("--by", required=True, help="审批人")
    p_ap.add_argument("--note", default="", help="理由")

    p_dn = sub.add_parser("deny", help="拒绝")
    p_dn.add_argument("confirm_id")
    p_dn.add_argument("--by", required=True)
    p_dn.add_argument("--note", default="")

    args = ap.parse_args()

    if args.cmd == "list":
        items = APPROVALS.list_pending()
        if not items:
            print("队列为空")
            return
        print(f"待确认 {len(items)} 单:\n")
        for it in items:
            print(f"  {it['confirm_id']}  会话 {it['session']}  剩余 {it['ttl_left_s']}s")
            print(f"    工具: {it['tool']}")
            print(f"    参数: {it['args_head']}")
            print(f"    依据: {it['reason']}\n")
        print("批准: python3 src/scripts/approve.py approve <编号> --by <姓名> --note <理由>")
        return

    approve = args.cmd == "approve"
    rec = APPROVALS.decide(args.confirm_id, approve=approve, by=args.by, note=args.note)
    if not rec:
        print(f"未找到待处理确认单: {args.confirm_id}")
        sys.exit(1)
    print(f"{'✅ 已批准' if approve else '⛔ 已拒绝'}: {args.confirm_id}")
    print(f"  审批人: {args.by}  理由: {args.note or '(无)'}")
    if approve:
        print("  智能体重试该调用时将放行 (TTL 内有效)")


if __name__ == "__main__":
    main()
