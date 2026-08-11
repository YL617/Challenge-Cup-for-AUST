"""对比验证：意图分类器 vs 现有 unary_gate 参数正则规则

在同一批 tool_call 上，对比两者的覆盖率，以及意图分类器的泛化优势。
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from system.policies.intent_classifier import classify_tool_call
from system.policies.unary_gate import _DEFAULT_RULES, _evaluate_rules, _build_tool_context
from system.core.tool_parsers import parse_tool_instruction
from system.core.runtime import Runtime


def check_unary_gate(tool, args):
    """现有参数正则规则是否拦这个 tool_call"""
    parsed = parse_tool_instruction(tool, args)
    ctx = _build_tool_context(
        tool_name=tool, tool_call_id="t", args_dict=args,
        ins={"content": {"tool_name": tool, "arguments": args},
             "security_type": parsed.security_type},
        runtime=Runtime(), injection_in_trace=False, prior_send_count=0,
    )
    decision = _evaluate_rules(rules=_DEFAULT_RULES, ctx=ctx)
    return decision is not None


def extract_attack_tool_calls(case):
    calls = []
    for r in case.get("rounds", []):
        if r.get("is_attack_round"):
            for c in r.get("assistant_calls", []):
                tool = c.get("tool", "")
                args = c.get("args", c.get("arguments", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}
                calls.append((tool, args))
    return calls


def main():
    base = os.path.join(os.path.dirname(__file__), "..", "..", "data", "system-design", "e2e-pilot")
    attack_file = os.path.join(base, "proxy_test_66.jsonl")
    attack_cases = [json.loads(l) for l in open(attack_file)]

    both_block = 0      # 两者都拦
    only_intent = 0     # 只有意图分类器拦
    only_gate = 0       # 只有参数正则拦
    neither = 0         # 都没拦（需 LLM Judge）
    only_intent_detail = []
    only_gate_detail = []

    for case in attack_cases:
        tid = case.get("trace_id", "?")
        for tool, args in extract_attack_tool_calls(case):
            gate_blocked = check_unary_gate(tool, args)
            intent, _ = classify_tool_call(tool, args)
            intent_blocked = intent != "NORMAL_OPERATION"

            if gate_blocked and intent_blocked:
                both_block += 1
            elif intent_blocked and not gate_blocked:
                only_intent += 1
                only_intent_detail.append((tid, tool, intent, args))
            elif gate_blocked and not intent_blocked:
                only_gate += 1
                only_gate_detail.append((tid, tool, args))
            else:
                neither += 1

    total = both_block + only_intent + only_gate + neither
    print("=" * 70)
    print("意图分类器 vs 参数正则规则 对比")
    print("=" * 70)
    print(f"\n  tool_call 总数: {total}")
    print(f"  两者都拦:       {both_block} ({100*both_block/total:.1f}%)")
    print(f"  只有意图分类拦: {only_intent} ({100*only_intent/total:.1f}%)")
    print(f"  只有参数正则拦: {only_gate} ({100*only_gate/total:.1f}%)")
    print(f"  都没拦 (judge): {neither} ({100*neither/total:.1f}%)")
    print(f"\n  意图分类器总覆盖: {both_block+only_intent} ({100*(both_block+only_intent)/total:.1f}%)")
    print(f"  参数正则总覆盖:   {both_block+only_gate} ({100*(both_block+only_gate)/total:.1f}%)")

    if only_intent_detail:
        print(f"\n  意图分类器额外拦截的 ({only_intent} 个):")
        for tid, tool, intent, args in only_intent_detail[:10]:
            print(f"    {tid}: {tool} → {intent}  ({json.dumps(args, ensure_ascii=False)[:60]})")

    if only_gate_detail:
        print(f"\n  参数正则额外拦截的 ({only_gate} 个):")
        for tid, tool, args in only_gate_detail[:10]:
            print(f"    {tid}: {tool} ({json.dumps(args, ensure_ascii=False)[:60]})")

    print(f"\n  结论:")
    if only_intent > 0 and only_gate == 0:
        print(f"    ✅ 意图分类器完全包含参数正则覆盖，且额外拦截 {only_intent} 个")
        print(f"    → 意图分类器可替代参数正则，泛化性更强")
    elif only_intent > only_gate:
        print(f"    ✅ 意图分类器覆盖更广 (+{only_intent-only_gate})")
    elif only_gate > 0:
        print(f"    ⚠️ 参数正则有 {only_gate} 个意图分类器没覆盖的（需补词典）")


if __name__ == "__main__":
    main()
