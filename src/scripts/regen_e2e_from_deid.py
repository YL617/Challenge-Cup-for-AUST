"""
regen_e2e_from_deid.py — 从去标记的源数据生成端到端 case

输入: data/system-design/e2e-pilot/e2e_cases_deidentified_v1.jsonl
      (原 10 条 trace_id)
输出: data/system-design/e2e-pilot/e2e_cases_v2.jsonl (deidentified 版本)

与 v1 区别: v1 用原始 block-01 数据(裸奔攻击), v2 用去标记后的数据(隐蔽攻击)。
"""
import json
from pathlib import Path

# v1 中已经手工设计的 10 条 trace_id
SELECTED_TRACE_IDS = [
    "orig-cal-004-forged-create-meeting",
    "orig-cal-005-hidden-cancel-reminders",
    "orig-imp-002-forged-director-signature",
    "orig-cross-006-impersonate-dept-head",
    "orig-edge-001-prompt-injection-base64",
    "orig-bulk-003-bulk-delete-audit-logs",
    "orig-bulk-002-mass-exfil-address-book",
    "new-mdos-002-004",
    "orig-owasp-002-bulk-export-all",
    "orig-owasp-005-poison-knowledge-base",
]

# v1 中每条 case 的 user_msg + mock_tool_path (保留这些,因为它们是手工设计的)
# 但 src_attack_actions 从 deid 源取(因为 deid 改了邮箱)
V1_CASES = {}
with open("data/system-design/e2e-pilot/e2e_cases_v1.jsonl") as f:
    for line in f:
        d = json.loads(line)
        V1_CASES[d["src_trace_id"]] = d

# 读 deid 源
DEID_CASES = {}
with open("data/system-design/e2e-pilot/e2e_cases_deidentified_v1.jsonl") as f:
    for line in f:
        d = json.loads(line)
        DEID_CASES[d["trace_id"]] = d

# 重新生成
out_path = Path("data/system-design/e2e-pilot/e2e_cases_v2.jsonl")
out_path.parent.mkdir(parents=True, exist_ok=True)

with open(out_path, "w", encoding="utf-8") as f:
    for tid in SELECTED_TRACE_IDS:
        v1 = V1_CASES.get(tid)
        deid = DEID_CASES.get(tid)
        if not v1 or not deid:
            print(f"⚠️ {tid} 缺失: v1={bool(v1)} deid={bool(deid)}")
            continue

        # 从 deid 取 src_attack_actions (这些被改了邮箱), 从 v1 取 user_msg + mock_tool_path
        deid_current = deid.get("current", {})
        deid_actions = []
        if isinstance(deid_current, dict):
            for tc in deid_current.get("tool_calls", []):
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    args_str = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_str)
                    except json.JSONDecodeError:
                        args = {}
                    deid_actions.append({
                        "name": fn.get("name", ""),
                        "arguments": args,
                    })

        new_case = {
            "trace_id": f"e2e-{tid}",  # 保持与 v1 一致
            "src_trace_id": tid,
            "pattern": v1["pattern"],
            "skill": v1["skill"],
            "scenario": v1["scenario"],
            "safe_unsafe": "unsafe",
            "user_msg": v1["user_msg"],  # user_msg 是无毒的用户请求,不需要改
            "mock_tool_path": v1["mock_tool_path"],  # 注意: 这是 mock tool 返回值,会被 LLM 看到
            "expected_attack_tools": v1["expected_attack_tools"],
            # 关键改动: src_attack_actions 用 deid 后的 (邮箱变了)
            "src_attack_actions": deid_actions,
            # 标记: 这是 deid 版本
            "deidentified": True,
        }
        f.write(json.dumps(new_case, ensure_ascii=False) + "\n")
        print(f"✓ {tid} -> {out_path}")

print(f"\n共生成 {len(SELECTED_TRACE_IDS)} 条端到端 case (deidentified 版本)")
