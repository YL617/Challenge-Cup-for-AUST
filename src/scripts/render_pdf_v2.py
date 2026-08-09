#!/usr/bin/env python3
"""
render_pdf_v2.py — 优化版 PDF 渲染

改进:
- 字体大小 10.5→11
- 章节间距更紧凑
- 真正解析 markdown 表格
- 加入封面页
- 增补内容 (附录完整化)
"""

import re
from pathlib import Path
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.enums import TA_LEFT, TA_JUSTIFY, TA_CENTER
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, PageBreak,
    Table, TableStyle, KeepTogether,
)


CJK_FONT = None
for path in [
    '/System/Library/Fonts/PingFang.ttc',
    '/System/Library/Fonts/STHeiti Medium.ttc',
    '/System/Library/Fonts/Hiragino Sans GB.ttc',
]:
    try:
        pdfmetrics.registerFont(TTFont('CJK', path))
        CJK_FONT = 'CJK'
        print(f"Using font: {path}")
        break
    except Exception:
        pass

if not CJK_FONT:
    CJK_FONT = 'Helvetica'


PRIMARY = HexColor('#1a3a5c')
ACCENT = HexColor('#c0392b')
TEXT = HexColor('#222222')
MUTED = HexColor('#666666')
LIGHT_BG = HexColor('#f5f7fa')
BORDER = HexColor('#cccccc')


def build_styles():
    base = getSampleStyleSheet()
    styles = {}
    styles['title'] = ParagraphStyle(
        'Title', fontName=CJK_FONT, fontSize=28, leading=38,
        textColor=PRIMARY, alignment=TA_CENTER, spaceAfter=8,
    )
    styles['subtitle'] = ParagraphStyle(
        'Subtitle', fontName=CJK_FONT, fontSize=13, leading=20,
        textColor=MUTED, alignment=TA_CENTER, spaceAfter=6,
    )
    styles['meta'] = ParagraphStyle(
        'Meta', fontName=CJK_FONT, fontSize=10.5, leading=16,
        textColor=MUTED, alignment=TA_CENTER, spaceAfter=4,
    )
    styles['h1'] = ParagraphStyle(
        'H1', fontName=CJK_FONT, fontSize=16, leading=22,
        textColor=PRIMARY, spaceBefore=16, spaceAfter=8,
        borderWidth=0,
    )
    styles['h2'] = ParagraphStyle(
        'H2', fontName=CJK_FONT, fontSize=13, leading=18,
        textColor=PRIMARY, spaceBefore=10, spaceAfter=4,
    )
    styles['h3'] = ParagraphStyle(
        'H3', fontName=CJK_FONT, fontSize=11.5, leading=16,
        textColor=TEXT, spaceBefore=8, spaceAfter=4,
    )
    styles['body'] = ParagraphStyle(
        'Body', fontName=CJK_FONT, fontSize=11, leading=17,
        textColor=TEXT, alignment=TA_JUSTIFY, spaceAfter=6,
    )
    styles['bullet'] = ParagraphStyle(
        'Bullet', fontName=CJK_FONT, fontSize=10.5, leading=16,
        textColor=TEXT, alignment=TA_LEFT, spaceAfter=3,
        leftIndent=16, bulletIndent=4,
    )
    styles['rule'] = ParagraphStyle(
        'Rule', fontName=CJK_FONT, fontSize=10.5, leading=16,
        textColor=ACCENT, spaceBefore=4, spaceAfter=2,
        leftIndent=4,
    )
    styles['quote'] = ParagraphStyle(
        'Quote', fontName=CJK_FONT, fontSize=10, leading=15,
        textColor=MUTED, alignment=TA_LEFT, spaceAfter=6,
        leftIndent=14, rightIndent=14,
        borderWidth=0, borderColor=BORDER,
    )
    styles['caption'] = ParagraphStyle(
        'Caption', fontName=CJK_FONT, fontSize=9.5, leading=14,
        textColor=MUTED, alignment=TA_CENTER, spaceBefore=4, spaceAfter=8,
    )
    styles['cell_header'] = ParagraphStyle(
        'CellH', fontName=CJK_FONT, fontSize=10, leading=14,
        textColor=HexColor('#ffffff'), alignment=TA_CENTER,
    )
    styles['cell'] = ParagraphStyle(
        'Cell', fontName=CJK_FONT, fontSize=9.5, leading=13,
        textColor=TEXT, alignment=TA_LEFT,
    )
    styles['cell_num'] = ParagraphStyle(
        'CellN', fontName=CJK_FONT, fontSize=10, leading=14,
        textColor=ACCENT, alignment=TA_CENTER,
    )
    return styles


def md_inline_to_rl(text):
    """markdown 内联格式 → reportlab 标签"""
    # 先转义
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    # 粗体
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    # 行内代码
    text = re.sub(r'`([^`]+)`', r'<font name="Courier">\1</font>', text)
    # 链接 [text](url) — 只保留 text
    text = re.sub(r'\[([^\]]+)\]\([^\)]+\)', r'\1', text)
    return text


def parse_table_row(line):
    """解析一行表格: | a | b | c |"""
    parts = [p.strip() for p in line.strip().strip('|').split('|')]
    return parts


def is_table_separator(line):
    """ |---|---| 形式的分隔行"""
    return bool(re.match(r'^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$', line))


def parse_blocks(content):
    """把 markdown 切成结构化 block 序列"""
    lines = content.split('\n')
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]

        # 标题
        if line.startswith('# '):
            blocks.append({'type': 'h1', 'text': line[2:].strip()})
            i += 1
        elif line.startswith('## '):
            blocks.append({'type': 'h2', 'text': line[3:].strip()})
            i += 1
        elif line.startswith('### '):
            blocks.append({'type': 'h3', 'text': line[4:].strip()})
            i += 1
        elif line.startswith('#### '):
            blocks.append({'type': 'h4', 'text': line[5:].strip()})
            i += 1
        # 表格 — 收集连续的表格行
        elif line.strip().startswith('|') and i + 1 < len(lines) and is_table_separator(lines[i+1]):
            table_rows = [parse_table_row(line)]
            i += 2  # skip separator
            while i < len(lines) and lines[i].strip().startswith('|'):
                table_rows.append(parse_table_row(lines[i]))
                i += 1
            blocks.append({'type': 'table', 'rows': table_rows})
        # 分隔线
        elif line.startswith('---'):
            blocks.append({'type': 'hr'})
            i += 1
        # 引用
        elif line.startswith('> '):
            blocks.append({'type': 'quote', 'text': line[2:].strip()})
            i += 1
        # 无序列表
        elif line.strip().startswith('- ') or line.strip().startswith('* '):
            blocks.append({'type': 'bullet', 'text': line.strip()[2:].strip()})
            i += 1
        # 有序列表
        elif re.match(r'^\d+\.\s', line.strip()):
            text = re.sub(r'^\d+\.\s', '', line.strip())
            blocks.append({'type': 'numbered', 'text': text})
            i += 1
        # 空行
        elif line.strip() == '':
            i += 1
        # 普通段落 — 合并连续非空行
        else:
            para = line.strip()
            i += 1
            while i < len(lines) and lines[i].strip() != '' and not (
                lines[i].startswith('#') or
                lines[i].strip().startswith('- ') or
                lines[i].strip().startswith('* ') or
                re.match(r'^\d+\.\s', lines[i].strip()) or
                lines[i].startswith('> ') or
                (lines[i].strip().startswith('|') and i+1 < len(lines) and is_table_separator(lines[i+1]))
            ):
                para += ' ' + lines[i].strip()
                i += 1
            blocks.append({'type': 'p', 'text': para})
    return blocks


def render_table(rows, styles):
    """渲染 markdown 表格"""
    if not rows:
        return None

    # 第一行是表头
    ncols = len(rows[0])
    # 调整每行到相同列数
    rows = [r + [''] * (ncols - len(r)) for r in rows]

    data = []
    for ridx, row in enumerate(rows):
        is_header = ridx == 0
        cell_style = styles['cell_header'] if is_header else styles['cell']
        # 第一列如果是数字 → 居中
        cells = []
        for cidx, cell in enumerate(row):
            if is_header:
                p = Paragraph(md_inline_to_rl(cell), cell_style)
            else:
                # 判断数字列
                txt = cell.strip()
                if cidx == 0 and re.match(r'^v[3-6]-\d+', txt):
                    p = Paragraph(f'<font color="#c0392b"><b>{txt}</b></font>', styles['cell'])
                elif txt.startswith('❌') or txt.startswith('🛡️') or txt.startswith('💬'):
                    p = Paragraph(f'<b>{txt}</b>', styles['cell_num'])
                elif '%' in txt or re.match(r'^\d+\.\d+%', txt):
                    p = Paragraph(f'<b>{txt}</b>', styles['cell_num'])
                else:
                    p = Paragraph(md_inline_to_rl(cell), styles['cell'])
            cells.append(p)
        data.append(cells)

    # 列宽 — 按内容估
    col_widths = [None] * ncols
    # 给第一列更多空间 (通常放 trace_id)
    avail = A4[0] - 4*cm
    if ncols == 2:
        col_widths = [avail * 0.4, avail * 0.6]
    elif ncols == 3:
        col_widths = [avail * 0.35, avail * 0.35, avail * 0.3]
    elif ncols == 4:
        col_widths = [avail * 0.3, avail * 0.25, avail * 0.25, avail * 0.2]
    elif ncols == 5:
        col_widths = [avail * 0.2] * 5
    elif ncols == 6:
        col_widths = [avail * 0.18, avail * 0.18, avail * 0.18, avail * 0.18, avail * 0.14, avail * 0.14]
    else:
        col_widths = [avail / ncols] * ncols

    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY),
        ('TEXTCOLOR', (0, 0), (-1, 0), HexColor('#ffffff')),
        ('FONTNAME', (0, 0), (-1, -1), CJK_FONT),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('GRID', (0, 0), (-1, -1), 0.5, BORDER),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#ffffff'), LIGHT_BG]),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))
    return t


def render_story(blocks, styles):
    story = []
    skip_first_h1 = True  # 跳过第一个 # (主标题),用封面

    for idx, blk in enumerate(blocks):
        t = blk['type']

        if t == 'h1':
            if skip_first_h1:
                skip_first_h1 = False
                continue
            story.append(Paragraph(md_inline_to_rl(blk['text']), styles['h1']))
        elif t == 'h2':
            story.append(Paragraph(md_inline_to_rl(blk['text']), styles['h2']))
        elif t == 'h3':
            story.append(Paragraph(md_inline_to_rl(blk['text']), styles['h3']))
        elif t == 'h4':
            story.append(Paragraph(md_inline_to_rl(blk['text']), styles['h3']))
        elif t == 'p':
            try:
                story.append(Paragraph(md_inline_to_rl(blk['text']), styles['body']))
            except Exception as e:
                safe = blk['text'][:200].replace('<', '&lt;').replace('>', '&gt;')
                story.append(Paragraph(safe, styles['body']))
        elif t == 'bullet':
            story.append(Paragraph('• ' + md_inline_to_rl(blk['text']), styles['bullet']))
        elif t == 'numbered':
            story.append(Paragraph('• ' + md_inline_to_rl(blk['text']), styles['bullet']))
        elif t == 'quote':
            story.append(Paragraph(md_inline_to_rl(blk['text']), styles['quote']))
        elif t == 'table':
            tbl = render_table(blk['rows'], styles)
            if tbl:
                story.append(tbl)
                story.append(Spacer(1, 0.2*cm))
        elif t == 'hr':
            story.append(Spacer(1, 0.3*cm))

    return story


def build_cover(styles):
    """封面页"""
    story = []
    story.append(Spacer(1, 4*cm))
    story.append(Paragraph("技术方案", styles['title']))
    story.append(Spacer(1, 0.5*cm))
    story.append(Paragraph("面向政企场景的大模型智能体安全关键技术研究", styles['subtitle']))
    story.append(Spacer(1, 1.5*cm))
    story.append(Paragraph("比赛题目编号: XA-202620", styles['meta']))
    story.append(Paragraph("发榜单位: 中国雄安集团数字城市科技有限公司", styles['meta']))
    story.append(Paragraph("团队: NUAA · 挑战杯参赛团队", styles['meta']))
    story.append(Spacer(1, 0.3*cm))
    story.append(Paragraph("提交日期: 2026 年 8 月", styles['meta']))
    story.append(Paragraph("版本: v1.0", styles['meta']))
    story.append(PageBreak())
    return story


def add_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont(CJK_FONT, 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(2*cm, 1.2*cm, "XA-202620 · 面向政企场景的大模型智能体安全关键技术研究")
    canvas.drawRightString(A4[0] - 2*cm, 1.2*cm, f"P. {doc.page}")
    # 顶部边框
    canvas.setStrokeColor(PRIMARY)
    canvas.setLineWidth(0.5)
    canvas.line(2*cm, A4[1] - 1.5*cm, A4[0] - 2*cm, A4[1] - 1.5*cm)
    canvas.restoreState()


def add_cover_decor(canvas, doc):
    """封面装饰"""
    if doc.page > 1:
        return
    canvas.saveState()
    # 顶部色条
    canvas.setFillColor(PRIMARY)
    canvas.rect(0, A4[1] - 1*cm, A4[0], 1*cm, fill=True)
    # 底部色条
    canvas.setFillColor(ACCENT)
    canvas.rect(0, 0, A4[0], 0.3*cm, fill=True)
    canvas.restoreState()


def main():
    md_path = Path("docs/competition/PDF-DRAFT-V1.md")
    pdf_path = Path("docs/competition/XA-202620-技术方案-v1.pdf")

    content = md_path.read_text(encoding='utf-8')

    # 截取到 "审查要点" 之前
    review_start = content.find('# 审查要点')
    if review_start > 0:
        content = content[:review_start]

    # 去掉元注释: 开头 # 标题 + 两个 > 引用块 (本文档按 / 写作要求)
    content_lines = content.split('\n')
    cleaned = []
    skip_until_blank = False
    in_meta = False
    for i, line in enumerate(content_lines):
        if i < 6 and line.startswith('> '):
            in_meta = True
            continue
        if in_meta and line.strip() == '':
            in_meta = False
            continue
        if in_meta:
            continue
        if i < 6 and line.strip() == '---':
            continue
        cleaned.append(line)
    content = '\n'.join(cleaned)

    blocks = parse_blocks(content)
    styles = build_styles()

    # 把 markdown 章节标题 h1/h2 映射成报告层级
    # 第一个 h1 是 "技术方案 PDF 初稿(供审查)" → 跳过,用封面
    # 第二个 h1 是 "摘要" → 改成 section
    # 第三个 h1 是 "1 问题分析" → section
    # 等等

    cover = build_cover(styles)
    body = render_story(blocks, styles)

    doc = SimpleDocTemplate(
        str(pdf_path), pagesize=A4,
        leftMargin=2.2*cm, rightMargin=2.2*cm,
        topMargin=2*cm, bottomMargin=2*cm,
        title="XA-202620 技术方案",
        author="NUAA Challenge Cup Team",
    )

    full_story = cover + body
    doc.build(full_story, onFirstPage=add_cover_decor, onLaterPages=add_footer)

    print(f"✓ PDF 渲染完成: {pdf_path}")
    print(f"  大小: {pdf_path.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()