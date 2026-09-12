"""EPUB 内容完整性校验(v0.3.2 P0-3)。

`verify_epub()` 证明的是「包结构合法」——容器、manifest、链接、CSS 都对。
它证明不了**内容有没有在链路里悄悄丢掉**:空章节、图片被吞、公式全部消失,
这些产物在结构上完全合法,打开却是一本坏书。

这里的做法是**拿产物和源头对照**:Markdown(work/book.md)是转换链路的真实内容源,
EPUB 由它生成。所以「Markdown 引用 87 张图、EPUB 里只有 12 张」这种断崖式差异
必须报出来 —— 只看 EPUB 自己是否自洽是发现不了的。

判定原则:**只在明显缩水时报 error**,轻微差异只报 warning。
宁可漏报,也不要把正常书判成坏书(否则用户会学会忽略这些提示)。
所有阈值都是「数量级」判断,不做逐字节比对。
"""
from __future__ import annotations

import re
from pathlib import Path

from epub.verify import VerifyResult, verify_epub
from page_result import page_marks

#: Markdown 图片引用:![alt](路径) —— 只取文件名去重(不同目录同名视为同一张)
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
#: 行间公式:$$ … $$ / \[ … \]
DISPLAY_MATH_RE = re.compile(r"\$\$.+?\$\$|\\\[.+?\\\]", re.S)
#: 行内公式:$ … $(排除 $$)
INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)[^$\n]{2,}?\$(?!\$)")

#: 文本量缩水到这个比例以下 → 报 error(0.4 = 只剩不到四成,不可能是正常差异)
TEXT_SHRINK_RATIO = 0.4
#: 文本量低于此比例才报「少于源」warning:Markdown 标记(表格竖线、星号等)的
#: 剥离差异只有几个百分点,不设门槛就会让每本书都带一条无用提示
TEXT_LESS_RATIO = 0.9
#: 公式保留比例低于此值 → 报 warning(生成方式差异不至于丢掉一半以上)
MATH_KEEP_RATIO = 0.5
#: 标题保留比例低于此值(且原文标题数 ≥ MIN) → 报 warning
HEADING_KEEP_RATIO = 0.8
HEADING_MIN_COUNT = 5
#: 页码覆盖率低于此值(仅在 Markdown 带页码注释时检查) → 报 warning
PAGE_COVER_RATIO = 0.9


def markdown_images(md: str) -> set[str]:
    """Markdown 引用的图片文件名集合(去重)。"""
    return {ref.split("/")[-1].split("\\")[-1] for ref in IMG_RE.findall(md)}


def markdown_math_count(md: str) -> int:
    """Markdown 里的公式数量(行间 + 行内)。行内 `$` 统计偏粗,只用于数量级判断。"""
    body = re.sub(r"```.*?```", "", md, flags=re.S)      # 代码块里的 $ 不算公式
    return len(DISPLAY_MATH_RE.findall(body)) + len(INLINE_MATH_RE.findall(body))


def markdown_text_chars(md: str) -> int:
    """Markdown 正文的非空白字符数(剥掉标记与图片引用)。"""
    text = re.sub(r"```.*?```", "", md, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)          # 页码注释
    text = IMG_RE.sub("", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)        # 链接留文字
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[*_`>|#]", "", text)
    return len(re.sub(r"\s+", "", text))


def markdown_heading_count(md: str) -> int:
    return len(re.findall(r"^#{1,6}\s+\S", md, flags=re.M))


def covered_pages(md: str) -> int:
    """Markdown 页码注释覆盖的页数(去重;没有注释则为 0)。"""
    pages: set[int] = set()
    for group in page_marks(md):
        pages.update(group)
    return len(pages)


def verify_content(
    markdown_path: str | Path,
    epub_path: str | Path,
    *,
    expected_pages: int | None = None,
) -> VerifyResult:
    """对照 Markdown 源检查 EPUB 的内容完整性,返回 VerifyResult。

    stats 里带 `content_` 前缀的计数供报告使用(源与产物的数量对照)。
    expected_pages: 原始 PDF 页数(有则检查页覆盖率;文本版 Markdown 没有页码
    注释时自动跳过)。
    """
    markdown_path = Path(markdown_path)
    epub_path = Path(epub_path)
    result = VerifyResult()

    try:
        md = markdown_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        result.readable = False
        result.fail("content_source_unreadable", f"无法读取 Markdown 源: {markdown_path} ({e})")
        return result

    structure = verify_epub(epub_path)
    if not structure.readable:
        result.readable = False
        result.fail("content_target_unreadable", f"EPUB 无法读取,无法做内容对照: {epub_path}")
        return result

    md_images = markdown_images(md)
    # 排除封面:封面由 pandoc 注入(源 Markdown 里没有),否则每本书都会多报一张
    epub_images = structure.stats.get("images_no_cover", structure.stats.get("images", 0))
    md_math = markdown_math_count(md)
    epub_math = structure.stats.get("math", 0)
    md_chars = markdown_text_chars(md)
    epub_chars = structure.stats.get("text_chars", 0)
    md_headings = markdown_heading_count(md)
    epub_headings = structure.stats.get("headings", 0)
    pages = covered_pages(md)

    result.stats.update({
        "content_md_images": len(md_images),
        "content_epub_images": epub_images,
        "content_md_math": md_math,
        "content_epub_math": epub_math,
        "content_md_chars": md_chars,
        "content_epub_chars": epub_chars,
        "content_md_headings": md_headings,
        "content_epub_headings": epub_headings,
        "content_covered_pages": pages,
    })

    # ---- 图片:少一张都是内容丢失(EPUB 多出来的通常是封面) ----
    if epub_images < len(md_images):
        result.fail("content_images_lost",
                    f"图片丢失: Markdown 引用 {len(md_images)} 张,EPUB 内只有 {epub_images} 张")
    elif epub_images > len(md_images):
        result.warn("content_images_extra",
                    f"EPUB 图片多于 Markdown 引用: {epub_images} 张 vs {len(md_images)} 张"
                    "(可能是封面或装饰图)")

    # ---- 公式:全丢 = error;丢一半以上 = warning ----
    if md_math and epub_math == 0:
        result.fail("content_math_lost",
                    f"公式全部丢失: Markdown 有 {md_math} 处公式,EPUB 内没有任何 MathML")
    elif md_math and epub_math < md_math * MATH_KEEP_RATIO:
        result.warn("content_math_shrunk",
                    f"公式疑似缩水: Markdown {md_math} 处 → EPUB {epub_math} 处 MathML")

    # ---- 文本量:断崖式缩水(EPUB 通常略多于 Markdown,因为含目录/标题页) ----
    if md_chars and epub_chars < md_chars * TEXT_SHRINK_RATIO:
        result.fail("content_text_shrunk",
                    f"正文疑似大量丢失: Markdown {md_chars} 字 → EPUB 正文 {epub_chars} 字")
    elif md_chars and epub_chars < md_chars * TEXT_LESS_RATIO:
        result.warn("content_text_less",
                    f"EPUB 正文明显少于 Markdown: {epub_chars} 字 vs {md_chars} 字"
                    f"({epub_chars / md_chars:.0%})")

    # ---- 标题:明显少了说明章节结构没落进产物 ----
    if md_headings >= HEADING_MIN_COUNT and epub_headings < md_headings * HEADING_KEEP_RATIO:
        result.warn("content_headings_lost",
                    f"标题数量减少: Markdown {md_headings} 个 → EPUB {epub_headings} 个")

    # ---- 页覆盖:只有 Markdown 带页码注释时才有意义(hybrid / 扫描分片) ----
    if expected_pages and pages and pages < expected_pages * PAGE_COVER_RATIO:
        result.warn("content_page_coverage",
                    f"页覆盖不足: 原始 PDF {expected_pages} 页,Markdown 页码注释只覆盖 {pages} 页")

    return result
