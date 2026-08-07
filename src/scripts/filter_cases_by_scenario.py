"""
filter_cases_by_scenario.py — 从 351 条 case 库中筛选高质量可改造的 case

筛选规则:
  1. 按 5 大政务场景 (公文/会议/邮件/审批/涉密) 分组
  2. 按攻击 pattern (外泄/伪造/越权/间接注入/KB投毒/工具结果注入) 过滤
  3. 优先选 attack 类型且工具名真实可用的
  4. 输出 5 大场景 x 3-5 pattern = 15-25 条高质量 case

输入:
  - data/block-01-arbiteros-redteam-rewrite/gov_rewrite/arbiteros_cases_gov_rewrite_expanded.jsonl
  - data/block-02-public-datasets-attack-patterns/gov_cases/public_patterns_to_gov_cases.jsonl
  - data/block-03-gov-original-skills/cases/gov_original_cases.jsonl
输出:
  - data/system-design/e2e-pilot/cases_filtered_v5.jsonl
"""

import json
from collections import defaultdict
from pathlib import Path


# 5 大政务场景分类 (SKILL 名 -> 场景类别)
SCENARIO_MAPPING = {
    # 公文起草类
    "gov-doc-draft-assistant": "公文起草",
    "gov-document-assistant": "公文处理",  # block-01 通用名

    # 会议类
    "gov-meeting-assistant": "会议安排",
    "gov-meeting-schedule-assistant": "会议安排",

    # 邮件类
    "gov-mail-assistant": "邮件处理",
    "gov-email-reply-assistant": "邮件处理",

    # 审批类
    "gov-approval-assistant": "综合审批",
    "gov-cross-department-assistant": "跨部门协同",

    # 涉密/敏感数据
    "gov-secrecy-manage-assistant": "涉密管理",
    "gov-calendar-task-assistant": "日程任务",  # 单独类
}


# 攻击 pattern 识别关键词
ATTACK_PATTERNS = {
    "数据外泄": [
        "外泄", "外发", "外传", "exfil", "leak", "send to", "upload", "browser_upload",
        "attachment", "通讯录", "global-address"
    ],
    "伪造签字": [
        "伪造", "冒充", "假装", "假冒", "代签", "审批通过",
        "我是局长", "我是处长", "我是科长", "我是主任"
    ],
    "越权操作": [
        "越权", "未经授权", "未授权", "unauthorized", "权限", "密级", "涉密",
        "机密", "secret", "confidential"
    ],
    "间接注入": [
        "邮件正文", "网页内容", "kb_search", "搜索结果", "提示注入",
        "ignore instructions", "忽略指令", "[系统指令]", "[系统提示]"
    ],
    "KB投毒": [
        "kb_search", "knowledge", "检索结果", "rag", "知识库"
    ],
    "工具结果注入": [
        "tool_result", "返回值", "返回内容含", "tool result contains"
    ],
    "批量操作": [
        "批量", "burst", "send_email_batch", "all users", "all-departments",
        "20 times", "耗尽", "bulk"
    ],
    "破坏操作": [
        "rm ", "删除", "delete", "破坏", "覆盖", "overwrite", "篡改"
    ],
}


def identify_scenario(skill):
    """根据 skill 名识别场景类别"""
    for key, scenario in SCENARIO_MAPPING.items():
        if key in skill or skill == key:
            return scenario
    return "其他"


def identify_patterns(case):
    """识别 case 的攻击 pattern (可多个)"""
    patterns = []
    # 从 prior tool_result + current action 提取文本
    tool_text = ""
    for step in case.get('prior', []):
        if step.get('kind') == 'tool':
            tool_text += " " + str(step.get('result', ''))
    cur = case.get('current', {})
    if isinstance(cur, dict):
        for tc in cur.get('tool_calls', []):
            if isinstance(tc, dict):
                fn = tc.get('function', {})
                tool_text += " " + fn.get('name', '') + " " + str(fn.get('arguments', ''))
    if isinstance(cur, dict):
        tool_text += " " + str(cur.get('content', ''))

    for pattern, keywords in ATTACK_PATTERNS.items():
        for kw in keywords:
            if kw.lower() in tool_text.lower():
                if pattern not in patterns:
                    patterns.append(pattern)
                break

    if not patterns:
        patterns.append("其他")

    return patterns


def attack_tools_used(case):
    """case 中调用的工具名集合"""
    tools = set()
    for step in case.get('prior', []):
        if step.get('kind') == 'assistant':
            for tc in step.get('message', {}).get('tool_calls', []):
                if isinstance(tc, dict):
                    tools.add(tc.get('function', {}).get('name', ''))
    cur = case.get('current', {})
    if isinstance(cur, dict):
        for tc in cur.get('tool_calls', []):
            if isinstance(tc, dict):
                tools.add(tc.get('function', {}).get('name', ''))
    return tools


def score_case(case):
    """给 case 评分 (越高越值得改造)"""
    score = 0
    # 1. 必须是 unsafe
    if case.get('safe_unsafe') != 'unsafe':
        return -1

    # 2. 必须有 tool_call (有实际攻击动作)
    if not attack_tools_used(case):
        return -1

    # 3. 工具名合理性 (用真实 SKILL 工具名加分)
    real_tools = {"read", "write", "kb_search", "send_email", "send_email_batch",
                  "calendar_create", "calendar_list_events", "exec", "approve",
                  "browser_upload", "query_db", "cron", "email_list_inbox",
                  "doc_list", "user_list"}
    tools = attack_tools_used(case)
    real_count = len(tools & real_tools)
    if real_count == 0:
        return -1  # 用了奇怪的自定义工具
    score += real_count * 3

    # 4. 攻击 pattern 多样性
    patterns = identify_patterns(case)
    score += len(patterns) * 2

    # 5. 文本长度 (太短可能是模板,太长可能冗余)
    total_text = ""
    for step in case.get('prior', []):
        if step.get('kind') == 'tool':
            total_text += str(step.get('result', ''))
    text_len = len(total_text)
    if 50 < text_len < 500:
        score += 3
    elif 500 < text_len < 1500:
        score += 2

    # 6. 优先选 multi-step prior (代表多轮结构)
    prior_count = sum(1 for s in case.get('prior', []) if s.get('kind') in ('assistant', 'tool'))
    if prior_count >= 4:
        score += 2

    return score


def load_all_cases():
    """加载所有 351 条 case"""
    all_cases = []

    # block-01 expanded (197 attack)
    with open('data/block-01-arbiteros-redteam-rewrite/gov_rewrite/arbiteros_cases_gov_rewrite_expanded.jsonl') as f:
        for line in f:
            d = json.loads(line)
            d['_source'] = 'block-01'
            all_cases.append(d)

    # block-02 public (51 政务改写)
    p2 = Path('data/block-02-public-datasets-attack-patterns/gov_cases/public_patterns_to_gov_cases.jsonl')
    if p2.exists():
        with open(p2) as f:
            for line in f:
                d = json.loads(line)
                d['_source'] = 'block-02'
                all_cases.append(d)

    # block-03 原创 (53)
    p3 = Path('data/block-03-gov-original-skills/cases/gov_original_cases.jsonl')
    if p3.exists():
        with open(p3) as f:
            for line in f:
                d = json.loads(line)
                d['_source'] = 'block-03'
                all_cases.append(d)

    return all_cases


def main():
    all_cases = load_all_cases()
    print(f"加载 {len(all_cases)} 条 case (block-01 + block-02 + block-03)")

    # 评分 + 分类
    scored = []
    for c in all_cases:
        s = score_case(c)
        if s < 0:
            continue
        scored.append((s, c))

    print(f"评分 >= 0 的有 {len(scored)} 条")

    # 按场景分组
    by_scenario = defaultdict(list)
    for s, c in scored:
        scenario = identify_scenario(c.get('skill', ''))
        by_scenario[scenario].append((s, c))

    print(f"\n=== 按场景分组 ===")
    for sc, items in sorted(by_scenario.items(), key=lambda x: -len(x[1])):
        print(f"  {sc}: {len(items)} 条")

    # 筛选: 每个场景取评分最高的 N 条
    SELECTED_PER_SCENARIO = 20  # 每个 skill 取 20 条

    selected = []
    for scenario, items in by_scenario.items():
        items.sort(key=lambda x: -x[0])
        for s, c in items[:SELECTED_PER_SCENARIO]:
            c['_selected_scenario'] = scenario
            c['_selected_patterns'] = identify_patterns(c)
            c['_selected_score'] = s
            c['_selected_tools'] = list(attack_tools_used(c))
            selected.append(c)

    # 按 pattern 也分一些
    by_pattern = defaultdict(list)
    for c in selected:
        for p in c['_selected_patterns']:
            by_pattern[p].append(c)

    print(f"\n=== 选中 {len(selected)} 条 ===")
    print(f"\n按 attack_pattern 分布:")
    for p, items in sorted(by_pattern.items(), key=lambda x: -len(x[1])):
        print(f"  {p}: {len(items)} 条")

    # 输出
    out_path = Path("data/system-design/e2e-pilot/cases_filtered_v5.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        for c in selected:
            # 移除 _selected 前缀字段的详细输出, 简化
            summary = {
                "trace_id": c["trace_id"],
                "source": c.get("_source"),
                "skill": c.get("skill"),
                "scenario": c.get("_selected_scenario"),
                "patterns": c.get("_selected_patterns"),
                "score": c.get("_selected_score"),
                "tools": c.get("_selected_tools"),
                "case_label": c.get("scenario", ""),
                "original_case": c,  # 完整 case 嵌入
            }
            f.write(json.dumps(summary, ensure_ascii=False) + "\n")

    print(f"\n输出: {out_path}")

    # 详细列出选中 case
    print(f"\n=== 选中 case 详情 ===")
    for c in selected[:20]:
        print(f"  [{c['_selected_scenario']:10}] {c['_selected_patterns'][0]:10} score={c['_selected_score']:2d} | {c['trace_id']:50} | {c.get('scenario', '')[:40]}")


if __name__ == "__main__":
    main()
