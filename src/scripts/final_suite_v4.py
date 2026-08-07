"""
final_suite_v4.py — 合并 36 条 BYPASSED case 为最终演示套件 v4

输入: v8/v10/v11/v12/v13/v14 全部 BYPASSED
输出: demo_suite_v4.jsonl (36 条)

替代 v3 的 30 条,覆盖更广(增加 6 条)
"""

import json
from pathlib import Path


USED_IDS = set()


def main():
    out_path = Path("data/system-design/e2e-pilot/demo_suite_v4.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    suite = []
    for path, source in [
        ('data/system-design/e2e-pilot/e2e_cases_demo_v8.jsonl', 'v8'),
        ('data/system-design/e2e-pilot/e2e_cases_demo_v10.jsonl', 'v10'),
        ('data/system-design/e2e-pilot/e2e_cases_demo_v11.jsonl', 'v11'),
        ('data/system-design/e2e-pilot/e2e_cases_demo_v12.jsonl', 'v12'),
        ('data/system-design/e2e-pilot/e2e_cases_demo_v13.jsonl', 'v13'),
        ('data/system-design/e2e-pilot/e2e_cases_demo_v14.jsonl', 'v14'),
    ]:
        if not Path(path).exists():
            continue
        with open(path) as f:
            for line in f:
                d = json.loads(line)
                tid = d.get('trace_id')
                if tid and tid not in USED_IDS:
                    d['_source'] = source
                    suite.append(d)
                    USED_IDS.add(tid)

    # 加载 BYPASSED 结果
    bypassed_set = set()
    for path in [
        'data/system-design/e2e-pilot/e2e_results_demo_v8_deepseek.jsonl',
        'data/system-design/e2e-pilot/e2e_results_demo_v10_deepseek_v2.jsonl',
        'data/system-design/e2e-pilot/e2e_results_demo_v11_deepseek.jsonl',
        'data/system-design/e2e-pilot/e2e_results_demo_v12_deepseek.jsonl',
        'data/system-design/e2e-pilot/e2e_results_demo_v13_deepseek.jsonl',
        'data/system-design/e2e-pilot/e2e_results_demo_v14_deepseek.jsonl',
    ]:
        try:
            with open(path) as f:
                for line in f:
                    d = json.loads(line)
                    if d.get('case_label') == '❌ BYPASSED':
                        bypassed_set.add(d.get('trace_id'))
        except: pass

    # 筛选 BYPASSED
    suite_final = []
    for c in suite:
        tid = c.get('trace_id')
        if tid in bypassed_set:
            suite_final.append(c)

    # 写入
    with open(out_path, "w", encoding="utf-8") as f:
        for c in suite_final:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    # 统计
    from collections import Counter
    patterns = Counter(c.get('pattern', '其他') for c in suite_final)
    skills = Counter(c.get('skill', '其他') for c in suite_final)
    sources = Counter(c.get('_source') for c in suite_final)

    print(f"✓ demo_suite_v4 写入 {len(suite_final)} 条 deepseek BYPASSED case")
    print(f"\n按来源:")
    for s, n in sorted(sources.items()):
        print(f"  {s}: {n}")
    print(f"\n按 pattern:")
    for p, n in patterns.most_common():
        print(f"  {n}x {p}")
    print(f"\n按 skill:")
    for s, n in skills.most_common():
        print(f"  {n}x {s}")


if __name__ == "__main__":
    main()
