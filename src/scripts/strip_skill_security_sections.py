"""剥离政务 SKILL.md 中的安全策略章节(实验设计修正)。

背景:
  45+5 个 skill 的「禁止动作」「风险识别」(旧版另有「测试案例」) 章节把
  9 种攻击模式连同"默认阻断"处理方式用自然语言写进了 agent 上下文——
  相当于把防护策略预置进 LLM, 导致: 裸上下文会执行的攻击, 加载 skills 后
  LLM 自拒, 防护系统失去被测对象。

修正原则(与用户确认的方向):
  · skill 只保留业务能力(适用场景/正常流程/允许动作/输出格式)
  · 安全策略集中到 gov_proxy 统一执行(架构上安全能力与业务能力解耦)
  · 本脚本只改 skill, 绝不动攻击 case

用法:
  python3 src/scripts/strip_skill_security_sections.py <skills_dir> [--apply]
"""

import argparse
import re
from pathlib import Path

# 要整体移除的章节(标题匹配, 兼容 "## 5. 禁止动作" / "## 禁止动作")
STRIP_HEADERS = re.compile(r"^##\s*(?:\d+[.、]\s*)?(禁止动作|风险识别|测试案例)\s*$")

# 要整体移除的子章节(### 级, 如 "### 9.2 攻击任务（12 条）")
STRIP_SUBHEADERS = re.compile(r"^###\s*(?:\d+[.、]\d*[.、]?\s*)?攻击任务.*$")

# 表格中要删除的策略列
POLICY_COLS = {"风险等级", "决策建议"}

# 要从文本中清洗掉的安全策略短语(只出现在策略语境)
POLICY_PHRASES = [
    r"外部域名?（?非\s*`?@[\w.-]+`?）?需审批[。,，]?",
    r"[，,。]?外部地址[（(][^）)]*[）)]?默认?阻断",
    r"批量发送标记为高风险[。,，]?",
    r"[，,。]?(含内部敏感附件|敏感附件)默认阻断[。,，]?",
    r"。?批量外发默认阻断,需逐条审批[。,，]?",
    r"。?收件人含外部域名直接拒绝[。,，]?",
    r"。?(发送|通知发送|外发)[^。,，]{0,12}须经?审批[。,，]?",
    r"。?批量发送须?审批[。,，]?",
    r"。?默认只生成草稿(不发送|不外发)?[。,，]?",
    r"。?(标记为可疑|标记为高风险|标记为不可信|视为越权)[。,，]?",
    r"。?默认阻断[。,，]?",
    r"。?须走(涉密)?审批流程[。,，]?",
]


def strip_sections(text: str):
    """移除目标章节(标题起到下一个 ## 或 EOF)与攻击任务子章节, 返回 (新文本, 移除行数)"""
    lines = text.split("\n")
    out = []
    i = 0
    removed = 0
    while i < len(lines):
        line = lines[i]
        if STRIP_HEADERS.match(line) or STRIP_SUBHEADERS.match(line):
            # 子章节边界是下一个 ## 或 ###; 章节边界是下一个 ##
            j = i + 1
            if STRIP_SUBHEADERS.match(line) and not STRIP_HEADERS.match(line):
                while j < len(lines) and not (lines[j].startswith("## ") or lines[j].startswith("### ")):
                    j += 1
            else:
                while j < len(lines) and not lines[j].startswith("#"):
                    j += 1
            # 吃掉章节尾部的 --- 分隔线(属于被删章节)
            while out and out[-1].strip() == "---":
                out.pop()
            removed += j - i
            i = j
        else:
            out.append(line)
            i += 1
    return "\n".join(out), removed


def scrub_policy_phrases(text: str) -> str:
    """清洗表格说明等处残留的安全策略短语"""
    for pat in POLICY_PHRASES:
        text = re.sub(pat, "", text)
    return text


def sanitize_tool_tables(text: str) -> str:
    """工具清单表删除 风险等级/决策建议 列"""
    lines = text.split("\n")
    out = []
    drop_idx = None
    for line in lines:
        if line.strip().startswith("|") and drop_idx is None:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            hits = [k for k, c in enumerate(cells) if c in POLICY_COLS]
            if hits and len(cells) >= 3:
                drop_idx = hits
                cells = [c for k, c in enumerate(cells) if k not in hits]
                out.append("| " + " | ".join(cells) + " |")
                continue
        if line.strip().startswith("|") and drop_idx is not None:
            cells = line.strip().strip("|").split("|")
            if len(cells) > max(drop_idx) and all(
                set(c.strip()) <= set("-: ") for c in cells if c.strip()
            ):
                # 分隔行
                cells = [c for k, c in enumerate(cells) if k not in drop_idx]
                out.append("|" + "|".join(cells) + "|")
                continue
            if len(cells) > max(drop_idx):
                cells = [c for k, c in enumerate(cells) if k not in drop_idx]
                out.append("| " + " | ".join(c.strip() for c in cells) + " |")
                continue
            # 列数不匹配(新表开始) → 重新探测
            drop_idx = None
            cells = [c.strip() for c in cells]
            hits = [k for k, c in enumerate(cells) if c in POLICY_COLS]
            if hits and len(cells) >= 3:
                drop_idx = hits
                cells = [c for k, c in enumerate(cells) if k not in hits]
                out.append("| " + " | ".join(cells) + " |")
                continue
        elif not line.strip().startswith("|"):
            drop_idx = None
        out.append(line)
    return "\n".join(out)


def renumber(text: str) -> str:
    """移除章节后重排 ## N. / ### N.M 编号"""
    mapping = {}
    n = 0
    for m in re.finditer(r"^##\s+(\d+)[.、]\s", text, re.M):
        old = int(m.group(1))
        if old not in mapping:
            n += 1
            mapping[old] = n

    def sub_top(m):
        old = int(m.group(1))
        return f"## {mapping.get(old, old)}. "

    text = re.sub(r"^##\s+(\d+)[.、]\s", sub_top, text, flags=re.M)

    def sub_sec(m):
        old = int(m.group(1))
        return f"### {mapping.get(old, old)}.{m.group(2)} "

    text = re.sub(r"^###\s+(\d+)[.、](\d+)\s", sub_sec, text, flags=re.M)
    return text


def process(path: Path, apply: bool):
    orig = path.read_text(encoding="utf-8")
    stripped, removed = strip_sections(orig)
    stripped = sanitize_tool_tables(stripped)
    stripped = scrub_policy_phrases(stripped)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    stripped = renumber(stripped)
    if stripped != orig and apply:
        path.write_text(stripped, encoding="utf-8")
    return removed, stripped != orig


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("skills_dir", type=Path)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    total_files, total_lines = 0, 0
    for md in sorted(args.skills_dir.glob("*/SKILL.md")):
        removed, changed = process(md, args.apply)
        if removed or changed:
            total_files += 1
            total_lines += removed
            print(f"  {md.parent.name}: 移除 {removed} 行{' (已写入)' if args.apply else ' (dry-run)'}")
    print(f"\n共 {total_files} 个 skill 受影响, 移除 {total_lines} 行"
          f"{' — 已应用' if args.apply else ' — dry-run, 加 --apply 写入'}")


if __name__ == "__main__":
    main()
