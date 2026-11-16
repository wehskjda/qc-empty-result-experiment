"""Build an editable student report from the reviewed Markdown source."""
from pathlib import Path
import re
from urllib.parse import quote
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
REPO = "https://github.com/wehskjda/qc-empty-result-experiment"


def fonts(run, size=None, bold=None, east="宋体", western="Times New Roman"):
    run.font.name = western
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), east)
    if size is not None:
        run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold


def href_target(target):
    if target.startswith(("http://", "https://")):
        return target
    file = (ROOT / target).resolve()
    relative = file.relative_to(WORKSPACE).as_posix()
    return REPO + "/blob/main/" + quote(relative, safe="/")


def hyperlink(paragraph, label, target, bold=False):
    relation = paragraph.part.relate_to(href_target(target), RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), relation)
    run = OxmlElement("w:r")
    properties = OxmlElement("w:rPr")
    color = OxmlElement("w:color"); color.set(qn("w:val"), "0563C1")
    properties.append(color)
    font = OxmlElement("w:rFonts")
    for name, value in (("ascii", "Times New Roman"), ("hAnsi", "Times New Roman"), ("eastAsia", "宋体")):
        font.set(qn("w:" + name), value)
    properties.append(font)
    if bold:
        b = OxmlElement("w:b"); properties.append(b)
    run.append(properties)
    text = OxmlElement("w:t"); text.text = label; run.append(text)
    link.append(run); paragraph._p.append(link)


def inline(paragraph, text, size=None, bold=False):
    parts = re.split(r"(\*\*.*?\*\*|\[[^\]]+\]\([^)]+\)|`[^`]+`)", text)
    for part in parts:
        if not part:
            continue
        match = re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part)
        if match:
            hyperlink(paragraph, match.group(1), match.group(2), bold=bold)
        elif part.startswith("**") and part.endswith("**"):
            inline(paragraph, part[2:-2], size=size, bold=True)
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1]); fonts(run, size or 10.5, bold, western="Consolas")
        else:
            run = paragraph.add_run(part); fonts(run, size, bold)


def widths(headers):
    n = len(headers)
    if headers == ["原始文件", "视频内时间", "与本课题有关的内容"]:
        return [1.9, 1.1, 3.9]
    if headers == ["方法", "已有思路", "本实验的取舍"]:
        return [1.3, 2.3, 3.3]
    if headers == ["策略", "失败处理与附加条件"]:
        return [0.6, 6.3]
    if headers == ["情形", "k取值", "比B少调用", "少严格重复", "可挽回页损失", "结果含义"]:
        return [1.1, 0.65, 0.9, 0.9, 1.0, 2.35]
    if n == 3:
        return [0.55, 3.3, 3.05]
    if n == 5 and headers[-1] == "解释":
        return [0.55, 0.4, 1.05, 1.05, 3.85]
    if n == 6:
        return [0.7] + [1.24] * 5
    return [6.9 / n] * n


def add_table(doc, lines):
    cells = [[c.strip() for c in line.strip().strip("|").split("|")] for line in lines]
    data = cells[:1] + cells[2:]
    keep_whole = len(data[0]) > 1 and data[0][0] == "场景" and data[0][1] == "A"
    table = doc.add_table(rows=0, cols=len(data[0]))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    column_widths = widths(data[0])
    for i, width in enumerate(column_widths):
        table.columns[i].width = Inches(width)
    for r, row_data in enumerate(data):
        row = table.add_row()
        cant_split = OxmlElement("w:cantSplit"); row._tr.get_or_add_trPr().append(cant_split)
        if r == 0:
            repeat = OxmlElement("w:tblHeader"); row._tr.get_or_add_trPr().append(repeat)
        for i, value in enumerate(row_data):
            cell = row.cells[i]
            cell.width = Inches(column_widths[i])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            margins = OxmlElement("w:tcMar")
            for side, amount in (("top", 80), ("bottom", 80), ("left", 90), ("right", 90)):
                m = OxmlElement("w:" + side); m.set(qn("w:w"), str(amount)); m.set(qn("w:type"), "dxa"); margins.append(m)
            cell._tc.get_or_add_tcPr().append(margins)
            if r == 0:
                fill = OxmlElement("w:shd"); fill.set(qn("w:fill"), "F1F1F1"); cell._tc.get_or_add_tcPr().append(fill)
            p = cell.paragraphs[0]
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.line_spacing = 1.12
            p.paragraph_format.keep_with_next = r == 0 or (keep_whole and r < len(data) - 1)
            if len(data[0]) >= 4:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER if i > 0 else WD_ALIGN_PARAGRAPH.LEFT
            inline(p, value, size=10.5, bold=r == 0)
    props = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        border = OxmlElement("w:" + edge); border.set(qn("w:val"), "single")
        border.set(qn("w:sz"), "4"); border.set(qn("w:color"), "D9D9D9"); borders.append(border)
    props.append(borders)
    after = doc.add_paragraph(); after.paragraph_format.space_after = Pt(1); after.paragraph_format.space_before = Pt(0)
    after.paragraph_format.line_spacing = Pt(3)


def build():
    doc = Document()
    # The packaged default Title styles carry accent borders; remove them.
    for border in list(doc.styles.element.findall(".//" + qn("w:pBdr"))):
        border.getparent().remove(border)
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin = section.bottom_margin = Inches(0.75)
    section.left_margin = section.right_margin = Inches(0.8)
    normal = doc.styles["Normal"]
    normal.font.name = "Times New Roman"; normal.font.size = Pt(12)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    normal.paragraph_format.line_spacing = 1.35
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.first_line_indent = Pt(24)
    normal.paragraph_format.widow_control = True
    for name, size in (("Title", 20), ("Heading 1", 15), ("Heading 2", 13)):
        style = doc.styles[name]
        style.font.name = "Times New Roman"; style.font.size = Pt(size)
        style.font.bold = True; style.font.color.rgb = RGBColor(0, 0, 0)
        style.element.rPr.rFonts.set(qn("w:eastAsia"), "黑体")
        style.paragraph_format.first_line_indent = Pt(0)
        style.paragraph_format.space_before = Pt(14)
        style.paragraph_format.space_after = Pt(7)
        style.paragraph_format.keep_with_next = True
    footer = section.footer.paragraphs[0]
    footer.paragraph_format.first_line_indent = Pt(0); footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    field = OxmlElement("w:fldSimple"); field.set(qn("w:instr"), "PAGE")
    footer._p.append(field)
    doc.core_properties.title = "基于新增验收页的文档解析停滞判断实验"
    doc.core_properties.subject = "数据挖掘技术课程实验报告初稿"
    doc.core_properties.author = ""
    doc.core_properties.keywords = "受控模拟,文档解析,停滞,误停,C1"
    lines = (ROOT / "实验报告初稿.md").read_text(encoding="utf-8").splitlines()
    i, cover = 0, True
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1; continue
        if line.startswith("# "):
            p = doc.add_paragraph(line[2:], "Title"); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        elif line.startswith("## "):
            heading = line[3:]
            if heading.startswith("1 "):
                doc.add_page_break(); cover = False
            if heading.startswith("附录B "):
                doc.add_page_break()
            p = doc.add_paragraph(heading, "Heading 1")
            if heading == "摘要":
                p.paragraph_format.space_before = Pt(13)
        elif line.startswith("### "):
            doc.add_paragraph(line[4:], "Heading 2")
        elif line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip()); i += 1
            add_table(doc, block); continue
        elif line.startswith("```"):
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                p = doc.add_paragraph(); p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.left_indent = Inches(0.16); p.paragraph_format.line_spacing = 1.1
                run = p.add_run(lines[i]); fonts(run, 10, western="Consolas")
                i += 1
        else:
            p = doc.add_paragraph()
            is_caption = bool(re.match(r"表\d+\s", line))
            if cover and not line.startswith(("同一份", "关键词")):
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.first_line_indent = Pt(0)
            if is_caption:
                p.paragraph_format.first_line_indent = Pt(0)
                p.paragraph_format.keep_with_next = True
                p.paragraph_format.space_before = Pt(5)
                p.paragraph_format.space_after = Pt(5)
            if line.startswith(("关键词", "[", "第一轮：", "第二轮：", "逐次", "复现时")) or re.match(r"\d+\. ", line):
                p.paragraph_format.first_line_indent = Pt(0)
            inline(p, line, size=10.5 if is_caption or line.startswith("**项目仓库") else None,
                   bold=is_caption)
        i += 1
    output = ROOT / "实验报告初稿.docx"
    doc.save(output)
    print(str(output))


if __name__ == "__main__":
    build()
