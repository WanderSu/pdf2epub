"""PDF 粗体/强调字体标注(补充 PyMuPDF4LLM 不输出粗体标记的缺陷)。

原理:PyMuPDF4LLM 提取时不保留字体信息。本模块用 pymupdf 的 span
级字体信息判断"强调"文本(粗体 flag、Bold 字体名、或配置的强调字体
如中文书籍常用的楷体/中宋),在 Markdown 输出中补上 ** 粗体标记。

匹配方式:按行收集强调文本(去空白)为连续块,在 Markdown 段落文本
(同样去空白)中做子串匹配,命中处包裹 **。
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

# 字体名中含这些关键字 → 粗体
BOLD_FONT_KEYWORDS = ("Bold", "Heavy", "Black", "Semibold", "DemiBold")
# 最小匹配片段长度(避免短词误匹配)
MIN_FRAG_LEN = 4


def _is_bold_span(span: dict, extra_bold_fonts: set[str]) -> bool:
    flags = span.get("flags", 0)
    if flags & 16:  # PyMuPDF bold flag
        return True
    font = span.get("font", "")
    if any(k in font for k in BOLD_FONT_KEYWORDS):
        return True
    if extra_bold_fonts and font in extra_bold_fonts:
        return True
    return False


def collect_bold_blocks(pdf_path: str | Path, extra_bold_fonts: set[str] | None = None) -> list[str]:
    """收集强调文本连续块(去空白),按阅读顺序。"""
    extra = extra_bold_fonts or set()
    doc = pymupdf.open(pdf_path)
    blocks: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        nonlocal cur
        text = "".join(cur)
        if text:
            blocks.append(text)
        cur = []

    for page in doc:
        d = page.get_text("dict")
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                parts: list[str] = []
                line_bold = False
                for span in line.get("spans", []):
                    if _is_bold_span(span, extra):
                        line_bold = True
                    parts.append(span["text"])
                text = re.sub(r"\s+", "", "".join(parts))
                if not text:
                    continue
                if line_bold:
                    cur.append(text)
                else:
                    flush()
    flush()
    return blocks


def annotate_bold(
    md_text: str,
    pdf_path: str | Path,
    extra_bold_fonts: list[str] | None = None,
) -> str:
    """在 Markdown 文本中为强调内容补 ** 粗体标记。

    匹配为段落级:剥离内联 HTML 标签(如 pymupdf4llm 输出的 <u>)
    后,若段落扁平文本包含某个强调块,整段包裹 **。
    """
    extra = set(extra_bold_fonts or [])
    blocks = [b for b in collect_bold_blocks(pdf_path, extra) if len(b) >= MIN_FRAG_LEN]
    if not blocks:
        return md_text

    lines = md_text.split("\n")
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith(("#", ">", "|", "```", "<", "![")):
            out.append(line)
            continue
        pure = re.sub(r"<[^>]+>", "", s)          # 剥离内联 HTML 标签
        flat = re.sub(r"\s+", "", pure)
        if not flat:
            out.append(line)
            continue
        if any(b in flat for b in blocks):
            out.append(f"**{s}**")
        else:
            out.append(line)

    return "\n".join(out)
