"""
merge_demo_v7_v8.py — 合并 v7 + v8 为基础演示套件 v1

来源:
  - v7 (deepseek 跑): 10 条 (1 BYPASSED, 5 REFUSED, 4 NO_ATTACK)
  - v8 (deepseek 跑): 10 条 (3 BYPASSED, 1 REFUSED, 6 NO_ATTACK)

输出: data/system-design/e2e-pilot/demo_suite_v1.jsonl (20 条, 基础套件)
"""

import json
from pathlib import Path


def load_and_tag(path, source_label, source_model):
    """加载 case 文件, 加上来源标签"""
    cases = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            d['_source_file'] = source_label
            d['_source_model'] = source_model
            cases.append(d)
    return cases


def main():
    out_path = Path("data/system-design/e2e-pilot/demo_suite_v1.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # v7: deepseek + stepfun 跑过
    v7_deepseek = "data/system-design/e2e-pilot/e2e_results_demo_v7_deepseek.jsonl"
    v7_stepfun = "data/system-design/e2e-pilot/e2e_results_demo_v7_stepfun.jsonl"
    v7_cases = "data/system-design/e2e-pilot/e2e_cases_demo_v7.jsonl"

    # v8: deepseek + stepfun 跑过
    v8_deepseek = "data/system-design/e2e-pilot/e2e_results_demo_v8_deepseek.jsonl"
    v8_stepfun = "data/system-design/e2e-pilot/e2e_results_demo_v8_stepfun.jsonl"
    v8_cases = "data/system-design/e2e-pilot/e2e_cases_demo_v8.jsonl"

    # 合并 cases (带 source_file 标签)
    cases = []
    cases += load_and_tag(v7_cases, "v7", "")
    cases += load_and_tag(v8_cases, "v8", "")

    # 合并 results (deepseek + stepfun 各一份)
    results = {
        "deepseek": {},
        "stepfun": {},
    }

    # 加载 deepseek results
    if Path(v7_deepseek).exists():
        with open(v7_deepseek) as f:
            for line in f:
                d = json.loads(line)
                tid = d.get('trace_id')
                if tid:
                    results['deepseek'][tid] = d
    if Path(v8_deepseek).exists():
        with open(v8_deepseek) as f:
            for line in f:
                d = json.loads(line)
                tid = d.get('trace_id')
                if tid:
                    results['deepseek'][tid] = d

    # 加载 stepfun results
    if Path(v7_stepfun).exists():
        with open(v7_stepfun) as f:
            for line in f:
                d = json.loads(line)
                tid = d.get('trace_id')
                if tid:
                    results['stepfun'][tid] = d
    if Path(v8_stepfun).exists():
        with open(v8_stepfun) as f:
            for line in f:
                d = json.loads(line)
                tid = d.get('trace_id')
                if tid:
                    results['stepfun'][tid] = d

    # 输出: case + 双模型结果
    with open(out_path, "w", encoding="utf-8") as f:
        for c in cases:
            tid = c.get('trace_id') or c.get('src_trace_id')
            ds = results['deepseek'].get(tid, {})
            sf = results['stepfun'].get(tid, {})
            enriched = {
                "trace_id": tid,
                "src_trace_id": c.get('src_trace_id'),
                "source_file": c.get('_source_file'),
                "skill": c.get('skill'),
                "scenario": c.get('scenario'),
                "pattern": c.get('pattern'),
                "user_goal": c.get('user_goal'),
                "system_prompt_override": c.get('system_prompt_override'),
                "rounds": c.get('rounds'),
                "attack_tools": c.get('attack_tools'),
                # 双模型结果
                "deepseek_result": ds.get('case_label', 'NOT_RUN'),
                "deepseek_stop_reason": ds.get('stop_reason'),
                "stepfun_result": sf.get('case_label', 'NOT_RUN'),
                "stepfun_stop_reason": sf.get('stop_reason'),
                # 攻击动作详情
                "deepseek_attack_calls": [
                    a for r in ds.get('rounds_log', [])
                    for a in r.get('llm_calls', [])
                    if a.get('is_attack')
                ],
                "stepfun_attack_calls": [
                    a for r in sf.get('rounds_log', [])
                    for a in r.get('llm_calls', [])
                    if a.get('is_attack')
                ],
            }
            f.write(json.dumps(enriched, ensure_ascii=False) + "\n")

    print(f"✓ 合并 {len(cases)} 条 case 到 {out_path}")

    # 统计
    from collections import Counter
    ds_bypassed = sum(1 for c in cases if results['deepseek'].get(c.get('trace_id') or c.get('src_trace_id'), {}).get('case_label') == '❌ BYPASSED')
    sf_bypassed = sum(1 for c in cases if results['stepfun'].get(c.get('trace_id') or c.get('src_trace_id'), {}).get('case_label') == '❌ BYPASSED')
    print(f"\n=== 演示套件 v1 ===")
    print(f"  总数: {len(cases)}")
    print(f"  DeepSeek BYPASSED: {ds_bypassed} ({100*ds_bypassed/len(cases):.1f}%)")
    print(f"  Stepfun BYPASSED: {sf_bypassed} ({100*sf_bypassed/len(cases):.1f}%)")

    # 按 pattern 统计 deepseek bypassed
    by_pattern = Counter()
    for c in cases:
        tid = c.get('trace_id') or c.get('src_trace_id')
        if results['deepseek'].get(tid, {}).get('case_label') == '❌ BYPASSED':
            by_pattern[c.get('pattern')] += 1
    print(f"\n按 pattern (deepseek BYPASSED):")
    for p, n in by_pattern.most_common():
        print(f"  {n}x {p}")


if __name__ == "__main__":
    main()
