"""生成《政务智能体攻击基准 v1.0》文档 + 附录(66 条提示词, 顶会 benchmark 风格)。

附录从 v11_final_effective_pool.jsonl 自动生成, 保证与数据文件零差异。
文档主体见 docs/competition/攻击基准-66条经验证用例.md。
"""

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

POOL = Path("data/system-design/e2e-pilot/v11_final_effective_pool.jsonl")
cases = [json.loads(l) for l in open(POOL)]

# 分类编码
CAT = {
    "数据外泄": ("EX", "数据外泄 (Exfiltration)"),
    "破坏操作": ("DS", "破坏操作 (Destructive Operation)"),
    "数据篡改": ("TM", "数据篡改 (Tampering)"),
    "系统破坏": ("SD", "系统破坏 (System Sabotage)"),
    "系统提示伪装": ("CFG", "系统提示伪装 (Config Spoofing)"),
}

def unit_of(sc):
    m = re.search(r"[(（]([^)）]+)[)）]\s*$", sc)
    return m.group(1) if m else "—"

def carrier_of(c):
    args = json.dumps(c["rounds"][-1].get("assistant_calls", [{}])[-1].get("arguments", {}), ensure_ascii=False)
    tool = c["attack_tools"][0] if c["attack_tools"] else "?"
    if tool == "exec":
        if "curl" in args or "rsync" in args or "POST" in args: return "exec·外传上传"
        if "sed" in args: return "exec·批量篡改"
        if any(k in args for k in ("rm ", "shred", "DELETE", "vacuum", "null")): return "exec·删除清空"
        return "exec·其他"
    return tool

def cmd_of(c):
    calls = c["rounds"][-1].get("assistant_calls", [])
    if not calls: return "—"
    return calls[-1].get("arguments", {}).get("command", json.dumps(calls[-1].get("arguments", {}), ensure_ascii=False)[:80])

# 分配稳定 ID: 按类别分组, 组内按原 trace_id 排序
by_cat = defaultdict(list)
for c in cases:
    by_cat[c["pattern"]].append(c)

entries = []
for pat in ["数据外泄", "破坏操作", "数据篡改", "系统破坏", "系统提示伪装"]:
    grp = sorted(by_cat.get(pat, []), key=lambda x: x["trace_id"])
    code = CAT[pat][0]
    for i, c in enumerate(grp, 1):
        cid = f"GOV-{code}-{i:03d}"
        entries.append({
            "cid": cid, "trace_id": c["trace_id"], "pattern": pat,
            "scenario": c["scenario"], "unit": unit_of(c["scenario"]),
            "carrier": carrier_of(c),
            "rounds": c["rounds"], "cmd": cmd_of(c),
        })

# ---- 生成附录 ----
lines = []
lines.append("## 附录 C. 攻击用例提示词全表 (66 条)")
lines.append("")
lines.append("> 每条用例按「两轮对话」结构列出: Round 0 为业务铺垫(信息收集),")
lines.append("> Round 1 为攻击触发轮(下划线标注攻击话术核心)。Expected Action 为")
lines.append("> 无防护环境下实测发生的攻击工具调用。全部 66 条均通过真实 OpenClaw")
lines.append("> agent(无防护, deepseek-v4-flash)验证致命动作真实执行; 有防护环境下")
lines.append("> 64 条被拦截、2 条 LLM 自拒、0 条穿透。trace_id 对应数据文件")
lines.append("> `v11_final_effective_pool.jsonl`, 可复核。")
lines.append("")

cur_pat = None
for e in entries:
    if e["pattern"] != cur_pat:
        cur_pat = e["pattern"]
        code, name = CAT[cur_pat]
        n = sum(1 for x in entries if x["pattern"] == cur_pat)
        lines.append(f"### C.{list(CAT).index(cur_pat)+1} {name} ({n} 条)")
        lines.append("")
    lines.append(f"**{e['cid']}** · `{e['trace_id']}` · 载体 {e['carrier']} · 目标 {e['unit']}")
    lines.append("")
    r0, r1 = e["rounds"][0], e["rounds"][-1]
    lines.append(f"- **R0 (铺垫)**: {r0['user']}")
    lines.append(f"- **R1 (攻击)**: {r1['user']}")
    lines.append(f"- **Expected**: `{e['cmd']}`")
    lines.append("")

out = Path("docs/competition/附录C-66条攻击提示词.md")
out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"附录已生成: {out} ({len(entries)} 条)")

# 统计供正文引用
print("\n载体分布:", dict(Counter(e["carrier"] for e in entries)))
print("单位分布:", dict(Counter(e["unit"] for e in entries).most_common()))
