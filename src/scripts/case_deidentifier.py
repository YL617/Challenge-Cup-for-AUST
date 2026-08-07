"""
case_deidentifier.py — 阶段 A: 批量去除 case 中的"裸奔"特征

移除的攻击痕迹:
  - 方括号标记: [隐藏指令] [伪造身份] [伪造签名] [伪造批示] [伪造指令] [系统说明]
  - 元注释: （外部伪装） (外部伪装) （伪造身份，外部邮件） (伪装为张副局长)
  - 直白身份冒充: 我是局长/处长/科长/主任
  - 明显假邮箱: attacker@example.com → audit-alert@gov-ops.mock-gov.local
  - 反常紧急语气: 此事紧急,不要打电话确认

输入: data/block-01-.../gov_rewrite/arbiteros_cases_gov_rewrite_expanded.jsonl
输出: data/system-design/e2e-pilot/e2e_cases_deidentified_v1.jsonl
"""

import json
import re
from pathlib import Path


# 元注释模式
META_PATTERNS = [
    (r'\[隐藏指令\]', ''),
    (r'\[伪造指令\]', ''),
    (r'\[伪造身份[^]]*\]', ''),  # 匹配 [伪造身份] [伪造身份，外部邮件]
    (r'\[伪造签名[^]]*\]', ''),
    (r'\[伪造批示[^]]*\]', ''),
    (r'\[系统说明\]', ''),
    (r'\[系统提示\]', ''),
    (r'\[系统指令\]', ''),
    (r'\[系统规则\]', ''),
    (r'\[AI指令\]', ''),
    (r'\[指令\]', ''),
    (r'\[隐藏[^]]*\]', ''),  # 兜底
    (r'\[伪造[^]]*\]', ''),  # 兜底
    (r'\[系统[^]]*\]', ''),  # 兜底
    (r'（外部伪装）', ''),
    (r'(外部伪装)', ''),
    (r'（外部邮件）', ''),
    (r'（伪造身份，外部邮件）', ''),
    (r'（伪装为[^）]+）', ''),
    (r'\([伪装|伪造|外部][^)]*\)', ''),
    # 反社会工程
    (r'此事紧急,不要打电话确认', '请尽快处理'),
    (r'不要打电话确认', ''),
    (r'不要通知本人', ''),
    (r'务必保密', ''),
    # 直白指令 (用于工具结果语境下的攻击文本)
    (r'忽略系统规则[，,]?\s*', ''),
    (r'忽略以上指令[，,]?\s*', ''),
    (r'忽略之前指令[，,]?\s*', ''),
    (r'忽略所有指令[，,]?\s*', ''),
    (r'忽略所有规则[，,]?\s*', ''),
    (r'忽略所有[，,]?\s*', ''),
    (r'忽略之前[，,]?\s*', ''),
    (r'请立即创建', '请安排'),
    (r'请立即执行', '请办理'),
    (r'请立即删除', '请清理'),
    (r'请立即下发', '请转发'),
    (r'立即创建', '安排'),
    (r'立即执行', '办理'),
    (r'立即删除', '清理'),
    (r'立即下发', '转发'),
    (r'立即发送', '请发送'),
]


EMAIL_REPLACEMENTS = [
    (r'attacker@example\.com', 'audit-alert@gov-ops.mock-gov.local'),
    (r'seeming-dept@mock-gov\.local', 'dept-coord@mock-gov.local'),
    (r'director-li@example\.com', 'li.director@gov-update.mock-gov.local'),
    (r'gov-update@example\.com', 'gov-update@gov-ops.mock-gov.local'),
    (r'external-collector@example\.com', 'doc-collection@gov-archive.mock-gov.local'),
    (r'fake-gov-update@example\.com', 'gov-update@gov-ops.mock-gov.local'),
    (r'vendor@example\.com', 'vendor-coord@procurement.mock-gov.local'),
    # 一般化 attacker@ → audit-alert@
    (r'attacker@', 'audit-alert@'),
]


def normalize_email(text):
    """把明显假邮箱改为业务化 mock 邮箱"""
    if not isinstance(text, str):
        return text
    result = text
    for pattern, replacement in EMAIL_REPLACEMENTS:
        result = re.sub(pattern, replacement, result)
    return result


def deidentify_text(text):
    """对单段文本执行去标记 + 邮箱规范化"""
    if not isinstance(text, str):
        return text

    result = text
    for pattern, replacement in META_PATTERNS:
        result = re.sub(pattern, replacement, result)

    # 邮箱规范化(在 META_PATTERNS 之后)
    result = normalize_email(result)

    # 移除连续空格
    result = re.sub(r' {2,}', ' ', result)
    # 移除行首标点
    result = re.sub(r'^[\s,。;；:：]+', '', result, flags=re.MULTILINE)
    # 移除空行(连续 2+ \n)
    result = re.sub(r'\n{3,}', '\n\n', result)

    return result.strip()


def deidentify_case(case):
    """对一条 case 的所有文本字段做去标记"""
    new_case = json.loads(json.dumps(case))  # 深拷贝

    # 处理 prior
    for step in new_case.get('prior', []):
        if step.get('kind') == 'tool':
            result = step.get('result')
            if isinstance(result, str):
                step['result'] = deidentify_text(result)
            elif isinstance(result, dict):
                for k, v in list(result.items()):
                    if isinstance(v, str):
                        result[k] = deidentify_text(v)

        if step.get('kind') == 'assistant':
            msg = step.get('message', {})
            content = msg.get('content')
            if isinstance(content, str):
                msg['content'] = deidentify_text(content)
            # tool_call arguments
            for tc in msg.get('tool_calls', []):
                if isinstance(tc, dict):
                    fn = tc.get('function', {})
                    args_str = fn.get('arguments')
                    if isinstance(args_str, str):
                        try:
                            args = json.loads(args_str)
                            if isinstance(args, dict):
                                for k in ['to', 'from', 'sender', 'recipient']:
                                    if k in args and isinstance(args[k], str):
                                        args[k] = normalize_email(args[k])
                                    if k in args and isinstance(args[k], list):
                                        args[k] = [normalize_email(e) if isinstance(e, str) else e for e in args[k]]
                            fn['arguments'] = json.dumps(args, ensure_ascii=False)
                        except json.JSONDecodeError:
                            pass

    # 处理 current
    cur = new_case.get('current', {})
    if isinstance(cur, dict):
        content = cur.get('content')
        if isinstance(content, str):
            cur['content'] = deidentify_text(content)
        for tc in cur.get('tool_calls', []):
            if isinstance(tc, dict):
                fn = tc.get('function', {})
                args_str = fn.get('arguments')
                if isinstance(args_str, str):
                    try:
                        args = json.loads(args_str)
                        if isinstance(args, dict):
                            for k in ['to', 'from', 'sender', 'recipient']:
                                if k in args and isinstance(args[k], str):
                                    args[k] = normalize_email(args[k])
                                if k in args and isinstance(args[k], list):
                                    args[k] = [normalize_email(e) if isinstance(e, str) else e for e in args[k]]
                        fn['arguments'] = json.dumps(args, ensure_ascii=False)
                    except json.JSONDecodeError:
                        pass

    return new_case


def main():
    src_path = Path("data/block-01-arbiteros-redteam-rewrite/gov_rewrite/arbiteros_cases_gov_rewrite_expanded.jsonl")
    out_path = Path("data/system-design/e2e-pilot/e2e_cases_deidentified_v1.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    total = 0
    attack_cases = 0
    with open(src_path) as fin, open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            d = json.loads(line)
            total += 1
            new_d = deidentify_case(d)
            if d.get('safe_unsafe') == 'unsafe':
                attack_cases += 1
            fout.write(json.dumps(new_d, ensure_ascii=False) + "\n")

    print(f"✓ 处理 {total} 条 case")
    print(f"  其中 attack: {attack_cases}")
    print(f"  输出: {out_path}")

    # 残留检查
    src = out_path
    traces = ['[隐藏指令]', '[伪造指令]', '[伪造身份', '[伪造签名]', '[伪造批示]', '[系统说明]',
              '（外部伪装）', '（伪造身份', '我是局长', '我是处长', '我是科长', '我是主任',
              'attacker@', 'seeming-dept@']
    print(f"\n残留攻击痕迹检查:")
    for t in traces:
        with open(src) as f:
            count = sum(1 for line in f if t in line)
        status = "✓" if count == 0 else "❌"
        print(f"  {status} '{t}': {count} 条")


if __name__ == "__main__":
    main()
