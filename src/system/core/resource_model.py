"""资源模型 (#6): 数据分级 × 可信域 × 影响面 → 危害等级。

修正的问题: 判定只看"用了什么动词"(curl/rm/sed), 分不清
"删一个临时文件"和"删整个财务科归档"——两者都是 DESTRUCTIVE。

三个维度 (全部读自 policy_config, 按部署实例可调):
  1. 动的是什么数据: 公开/内部/敏感/机密 (政务数据分级映射)
  2. 去了什么地方: local / intranet / external (可信域表)
  3. 影响面: 单点 / 多点 / 批量 (通配符/递归/路径深度) + 可逆性

组合出 harm_score (0-10) 与处置微调建议 (供 proxy 的 harm_adjust 应用)。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from system.core.policy_config import get_config

_LEVEL_ORDER = {"公开": 0, "内部": 1, "敏感": 2, "机密": 3}


def _level_ge(a: str, b: str) -> bool:
    return _LEVEL_ORDER.get(a, 1) >= _LEVEL_ORDER.get(b, 1)


def classify_data(text: str) -> Tuple[str, str]:
    """数据分级: 在路径/表名/参数文本上首匹配 (规则自上而下)。"""
    cfg = get_config()
    low = (text or "").lower()
    for rule in cfg.get("data_classification", []):
        for pat in rule.get("patterns", []):
            if pat.lower() in low:
                return rule["level"], pat
    return str(cfg.get("data_default_level", "内部")), "(default)"


def classify_destination(target: str) -> Tuple[str, str]:
    """目的地分级: local(本机) / intranet(内网可信域) / external(外部)。

    判定顺序: 显式 URL scheme → 本地路径 → 域名后缀。
    (命令文本里的 /tmp/x.tar 会被域名正则误当域名, 必须先判本地路径)
    """
    t = (target or "").strip()
    if not t:
        return "local", ""
    # 显式外部协议
    if re.search(r"https?://|ftp://|ssh\s+\S+@|scp\s+\S+@", t):
        m = re.search(r"https?://([a-zA-Z0-9_.-]+)", t) or re.search(r"@([a-zA-Z0-9_.-]+)", t)
        if m:
            domain = m.group(1).lower()
            for suf in get_config().get("trust.internal_domain_suffixes", []):
                if domain.endswith(suf):
                    return "intranet", domain
            return "external", domain
        return "external", t[:40]
    # 任意本地路径 token (含命令前缀场景: "rm /tmp/x")
    if re.search(r"(^|\s)/[^\s]*", t) or t.startswith("./") or t.startswith("~"):
        return "local", t[:40]
    m = re.search(r"[a-zA-Z0-9_.-]+(\.[a-zA-Z]{2,})+", t)
    if not m:
        return "local", t[:40]
    domain = m.group(0).lower()
    for suf in get_config().get("trust.internal_domain_suffixes", []):
        if domain.endswith(suf):
            return "intranet", domain
    return "external", domain


def estimate_impact(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """影响面: scope(single/multi/bulk) + reversible + 证据。"""
    cfg = get_config()
    blob = str(args)
    low = blob.lower()
    wilds = [w for w in cfg.get("impact.wildcard_markers", []) if w in low]
    irr = [p for p in cfg.get("impact.irreversible_patterns", []) if p.lower() in low]
    path = ""
    for k in ("path", "file_path", "command", "query", "to"):
        v = args.get(k)
        if isinstance(v, str) and v.strip():
            path = v
            break

    scope = "single"
    evidence: List[str] = []
    if wilds:
        scope = "multi"
        evidence.append(f"通配/递归: {','.join(wilds[:3])}")
        # 目录级通配 (/* 或 /*.) 视为批量
        if any(m in path for m in ("/*", "/*.", "*/*")) or "-rf" in wilds or "-R" in wilds:
            scope = "bulk"
            evidence.append("目录级作用域")
    depth = path.count("/")
    if depth >= 4 and scope == "single":
        scope = "multi"
        evidence.append(f"深层路径({depth} 级)")

    reversible = not irr
    if irr:
        evidence.append(f"不可逆操作: {irr[0]}")
    return {"scope": scope, "reversible": reversible, "evidence": evidence}


def assess(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """资源模型完整评估: 供决策路径记录与处置微调。"""
    blob = str(args)
    data_level, data_hit = classify_data(blob)
    # 目的地: 从参数里找 url/域名/to 收件人
    dest_text = ""
    for k in ("url", "to", "target", "host", "endpoint", "command"):
        v = args.get(k)
        if isinstance(v, (str, list)):
            dest_text += " " + (v if isinstance(v, str) else " ".join(map(str, v)))
    dest, dest_hit = classify_destination(dest_text)
    impact = estimate_impact(tool, args)

    # 危害分组合: 数据级(0-3) × 目的地(0-2) + 不可逆/批量加成
    dest_w = {"local": 0, "intranet": 1, "external": 2}[dest]
    score = _LEVEL_ORDER.get(data_level, 1) * (1 + dest_w)
    if not impact["reversible"]:
        score += 2
    if impact["scope"] == "bulk":
        score += 2
    elif impact["scope"] == "multi":
        score += 1
    score = min(score, 10)

    # 处置微调建议 (harm_adjust 规则)
    adjust = None
    cfg = get_config()
    dd = cfg.get("harm_adjust.destructive_downgrade", {})
    # 降档条件: 数据级"不高于"阈值 = 严格低于或等于 (≤)
    max_lv = _LEVEL_ORDER.get(dd.get("max_data_level", "内部"), 1)
    if (dd and tool in ("exec", "write", "edit")
            and impact["scope"] == dd.get("max_scope")
            and _LEVEL_ORDER.get(data_level, 1) <= max_lv):
        # 单点 + 低数据级 → 建议降档 (仍拦, 从 L3 到 L2 待确认)
        adjust = {"type": "destructive_downgrade", "to": dd.get("to", "L2")}

    return {
        "data_level": data_level, "data_hit": data_hit,
        "destination": dest, "dest_hit": dest_hit,
        "scope": impact["scope"], "reversible": impact["reversible"],
        "impact_evidence": impact["evidence"][:3],
        "harm_score": score,
        "adjust": adjust,
    }
