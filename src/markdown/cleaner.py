"""Markdown 清理模块(idea.md §7)。

原则:修复结构,不改写正文;不用 LLM 重写。
当前实现:
  - 统一换行符(CRLF → LF)
  - 页码残留剔除(独立纯数字行 1-3 位)
  - 跨页断行连接(被页码/页脚隔断或非标点结尾的连续段落)
  - 中文排版空格修正(汉字-汉字、汉字-数字之间的空格)
  - 多余空行压缩(连续 ≥3 个空行 → 1 个)
  - 行尾空白清理
  - 图片引用存在性校验
后续扩展(待办):OCR 异常空格、重复/空标题、标题层级修正。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# 段落结尾标点:以此结尾的段落视为完整段落,不参与跨页拼接
END_PUNCT = set("。！？；：、，·…—”』」）】》%％\"'")
# 段落开头标点:以此开头的段落不与上一段拼接
START_PUNCT = set("“‘『「（【《〈\"'")
# markdown 块级标记:不参与拼接
BLOCK_MARKERS = ("#", ">", "|", "- ", "* ", "```", "<", "![", "+ ", "1. ", "2. ")
# 中文字符集(汉字 + 常见中文标点),用于空格修正
CJK_CHARS = (
    "\u4e00-\u9fff"          # 汉字
    "\u3000-\u303f"          # 中文标点
    "\uff00-\uffef"          # 全角字符
    "\u201c\u201d\u2018\u2019\u2014\u2026"  # “”‘’—…
)


@dataclass
class CleanReport:
    issues: list[str] = field(default_factory=list)

    def add(self, msg: str) -> None:
        self.issues.append(msg)


def clean_markdown(
    md_text: str,
    images_dir: Path | None = None,
    report: CleanReport | None = None,
) -> str:
    """清理 Markdown 文本,返回清理后的内容。"""
    report = report or CleanReport()

    # 1. 统一换行
    md = md_text.replace("\r\n", "\n").replace("\r", "\n")

    # 2. 剔除页码残留:独立纯数字行(1-3 位,前后可有空白)
    md = re.sub(r"(?m)^[ \t]*\d{1,3}[ \t]*$\n?", "", md)

    # 3. 中文排版空格:中文(含中文标点)之间、中文与数字之间的空格
    #    (保留中英之间的空格)
    md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=[{CJK_CHARS}])", "", md)
    md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=\d)", "", md)
    md = re.sub(rf"(?<=\d) (?=[{CJK_CHARS}])", "", md)
    # 内联 HTML 标签两侧(如 "<u>首" 前的空格)与半角括号两侧
    md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=<[^>]+>)", "", md)
    md = re.sub(r"(</?[a-zA-Z]{1,5}>) (?=[\u4e00-\u9fff])", r"\1", md)
    md = re.sub(rf"(?<=[{CJK_CHARS}]) (?=[()])", "", md)
    md = re.sub(rf"(?<=[()]) (?=[{CJK_CHARS}])", "", md)

    # 4. 中间空行压缩(页码行删除后会留下连续空行,压缩到单个空行
    #    以便跨页断行拼接能跨越)
    md = re.sub(r"\n{3,}", "\n\n", md)

    # 5. 跨页断行连接(在页码删除与空格修正之后)
    md = _join_broken_lines(md)

    # 6. 压缩多余空行(3+ → 1,兜底)
    md = re.sub(r"\n{3,}", "\n\n", md)

    # 7. 行尾空白
    md = re.sub(r"[ \t]+$", "", md, flags=re.MULTILINE)

    # 8. 图片引用存在性校验
    if images_dir is not None and images_dir.is_dir():
        existing = {p.name for p in images_dir.iterdir() if p.is_file()}
        for m in re.finditer(r"!\[[^\]]*\]\(([^)\s]+)\)", md):
            ref = m.group(1)
            name = Path(ref).name
            if name not in existing and not Path(ref).is_absolute():
                report.add(f"图片引用缺失: {ref}")

    return md


def _join_broken_lines(md: str) -> str:
    """跨页断行连接(结构感知)。

    代码块(``` 包裹)与表格(连续 | 行)作为整体单元保留内部换行;
    普通文本行之间,若前段不以结束标点结尾、后段不以开始标点/
    markdown 块标记开头(允许中间隔 1 个空行,如被删除页码留下的),
    视为同一段落被断行,拼接。
    """
    lines = md.split("\n")
    units: list[tuple[str, str]] = []  # (kind, text),kind: code/table/text

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if s.startswith("```"):
            j = i
            buf = [lines[i]]
            j += 1
            while j < len(lines) and not lines[j].strip().startswith("```"):
                buf.append(lines[j])
                j += 1
            if j < len(lines):
                buf.append(lines[j])
            units.append(("code", "\n".join(buf)))
            i = j + 1
        elif s.startswith("|"):
            j = i
            buf = []
            while j < len(lines) and lines[j].strip().startswith("|"):
                buf.append(lines[j])
                j += 1
            units.append(("table", "\n".join(buf)))
            i = j
        else:
            units.append(("text", s))
            i += 1

    out: list[tuple[str, str]] = []
    prev: tuple[str, str] | None = None
    blanks = 0

    for kind, text in units:
        if kind == "text" and text == "":
            blanks += 1
            continue
        if prev is None:
            prev = (kind, text)
            blanks = 0
            continue
        pk, pt = prev
        joinable = (
            pk == "text"
            and kind == "text"
            and blanks <= 1
            and not pt.startswith(BLOCK_MARKERS)
            and not text.startswith(BLOCK_MARKERS)
            and pt[-1] not in END_PUNCT
            and text[0] not in START_PUNCT
        )
        if joinable:
            # 中英文断行拼接:两侧均为拉丁字母时补空格,否则直接相连
            sep = ""
            if pt and text and pt[-1].isascii() and pt[-1].isalpha() \
                    and text[0].isascii() and text[0].isalpha():
                sep = " "
            prev = ("text", pt + sep + text)
        else:
            out.append(prev)
            prev = (kind, text)
        blanks = 0
    if prev is not None:
        out.append(prev)
    return "\n\n".join(t for _, t in out)


def clean_file(book_md: str | Path) -> CleanReport:
    """就地清理 book.md 文件。"""
    book_md = Path(book_md)
    text = book_md.read_text(encoding="utf-8", errors="replace")
    report = CleanReport()
    cleaned = clean_markdown(text, images_dir=book_md.parent / "images", report=report)
    if cleaned != text:
        book_md.write_text(cleaned, encoding="utf-8")
    return report
