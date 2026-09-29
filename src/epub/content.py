"""EPUB 内容完整性校验(v0.3.2 P0-3)。

`verify_epub()` 证明的是「包结构合法」——容器、manifest、链接、CSS 都对。
它证明不了**内容有没有在链路里悄悄丢掉**:空章节、图片被吞、公式全部消失,
这些产物在结构上完全合法,打开却是一本坏书。

这里的做法是**拿产物和源头对照**:Markdown(work/book.md)是转换链路的真实内容源,
EPUB 由它生成。所以「Markdown 引用 87 张图、EPUB 里只有 12 张」这种断崖式差异
必须报出来 —— 只看 EPUB 自己是否自洽是发现不了的。

对照项:图片 / 公式 / 正文 / 标题 / 页码覆盖 + **硬换行(诗行不成一行)** /
表格 / 代码块 / 脚注 / 链接 —— 后四类都是「结构上看不出来、内容却没了」的地方。

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
#: 围栏代码块(整段不属于正文,统计时要先摘掉)
FENCE_RE = re.compile(r"```.*?```", re.S)
#: Markdown 链接(排除图片):留文字做正文对照
MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)\)")
#: 硬换行:行尾两个以上空格(pandoc 的 Markdown 硬换行 = 阅读器里的真分行)。
#: 必须**后面还有一行内容**才算 —— 段末的行尾空格 pandoc 不渲染成 `<br>`,
#: 把它算进来就会平白报「硬换行减少」。
TRAILING_BREAK_RE = re.compile(r"\S {2,}\n(?=\S)")
#: 原始 HTML 换行标记(云端 OCR 的图片文字块用 `<br>` 分行)
RAW_BR_RE = re.compile(r"<br\b[^>]*>", re.I)
#: 表格分隔行(`|---|:--:|`)—— 出现一次就是一张表
TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", re.M)
#: 围栏代码块的开合行(成对出现 → 块数 = 行数 // 2)
FENCE_MARK_RE = re.compile(r"^\s*(?:```|~~~)", re.M)
#: 脚注定义:`[^label]: 内容`
FOOTNOTE_DEF_RE = re.compile(r"^\[\^([^\]\s]+)\]:", re.M)

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
#: 硬换行保留比例低于此值 → 报 warning(诗行会被压成一行,阅读器里看不出来源)
BREAK_KEEP_RATIO = 0.9


def markdown_images(md: str) -> set[str]:
    """Markdown 引用的图片文件名集合(去重)。"""
    return {ref.split("/")[-1].split("\\")[-1] for ref in IMG_RE.findall(md)}


def markdown_math_count(md: str) -> int:
    """Markdown 里的公式数量(行间 + 行内)。行内 `$` 统计偏粗,只用于数量级判断。"""
    body = FENCE_RE.sub("", md)                           # 代码块里的 $ 不算公式
    return len(DISPLAY_MATH_RE.findall(body)) + len(INLINE_MATH_RE.findall(body))


def markdown_text_chars(md: str) -> int:
    """Markdown 正文的非空白字符数(剥掉标记与图片引用)。"""
    text = FENCE_RE.sub("", md)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)          # 页码注释
    text = IMG_RE.sub("", text)
    text = MD_LINK_RE.sub(r"\1", text)                          # 链接留文字
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    text = re.sub(r"[*_`>|#]", "", text)
    return len(re.sub(r"\s+", "", text))


def markdown_heading_count(md: str) -> int:
    return len(re.findall(r"^#{1,6}\s+\S", md, flags=re.M))


def markdown_hard_breaks(md: str) -> int:
    """Markdown 里的硬换行数量(行尾两个以上空格 + 原始 HTML `<br>`)。

    诗行、图片文字块这类「一行一行」的内容靠它才在阅读器里真的分行 ——
    pandoc 会把段落内的软换行渲染成空格,所以数不到硬换行就意味着被压成一行了。
    """
    body = FENCE_RE.sub("", md)                           # 代码块里的换行不参与
    return len(TRAILING_BREAK_RE.findall(body)) + len(RAW_BR_RE.findall(body))


def markdown_table_count(md: str) -> int:
    """表格数量(按分隔行计;代码块里的竖线不算)。"""
    return len(TABLE_SEP_RE.findall(FENCE_RE.sub("", md)))


def markdown_code_block_count(md: str) -> int:
    """围栏代码块数量(开合行成对)。"""
    return len(FENCE_MARK_RE.findall(md)) // 2


def markdown_footnote_defs(md: str) -> int:
    """脚注定义数量(`[^1]: …`)。有定义就必须在产物里出现,否则是内容丢失。"""
    return len(set(FOOTNOTE_DEF_RE.findall(FENCE_RE.sub("", md))))


def markdown_link_count(md: str) -> int:
    """Markdown 链接数量(不含图片)。"""
    return len(MD_LINK_RE.findall(FENCE_RE.sub("", md)))


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
    md_breaks = markdown_hard_breaks(md)
    epub_breaks = structure.stats.get("hard_breaks", 0)
    md_tables = markdown_table_count(md)
    epub_tables = structure.stats.get("tables", 0)
    md_code = markdown_code_block_count(md)
    epub_code = structure.stats.get("code_blocks", 0)
    md_footnotes = markdown_footnote_defs(md)
    epub_footnotes = structure.stats.get("footnote_refs", 0)
    md_links = markdown_link_count(md)
    epub_links = structure.stats.get("links", 0)
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
        "content_md_breaks": md_breaks,
        "content_epub_breaks": epub_breaks,
        "content_md_tables": md_tables,
        "content_epub_tables": epub_tables,
        "content_md_code": md_code,
        "content_epub_code": epub_code,
        "content_md_footnotes": md_footnotes,
        "content_epub_footnotes": epub_footnotes,
        "content_md_links": md_links,
        "content_epub_links": epub_links,
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

    # ---- 硬换行:诗行 / 分行内容被压成一行就数不到 <br> 了 ----
    if md_breaks and epub_breaks < md_breaks * BREAK_KEEP_RATIO:
        result.warn("content_breaks_lost",
                    f"硬换行减少: Markdown {md_breaks} 处 → EPUB {epub_breaks} 处"
                    "(诗行等分行内容在阅读器里会被压成一行)")

    # ---- 表格 / 代码块:全丢是内容缺失,少一部分只告警 ----
    if md_tables and epub_tables == 0:
        result.fail("content_tables_lost",
                    f"表格全部丢失: Markdown 有 {md_tables} 张表,EPUB 内没有任何 <table>")
    elif md_tables and epub_tables < md_tables:
        result.warn("content_tables_shrunk",
                    f"表格数量减少: Markdown {md_tables} 张 → EPUB {epub_tables} 张")
    if md_code and epub_code == 0:
        result.fail("content_code_lost",
                    f"代码块全部丢失: Markdown 有 {md_code} 个代码块,EPUB 内没有任何 <pre>")
    elif md_code and epub_code < md_code:
        result.warn("content_code_shrunk",
                    f"代码块数量减少: Markdown {md_code} 个 → EPUB {epub_code} 个")

    # ---- 脚注:有定义却没有脚注引用 = 注释整批消失 ----
    if md_footnotes and epub_footnotes == 0:
        result.fail("content_footnotes_lost",
                    f"脚注全部丢失: Markdown 有 {md_footnotes} 条脚注定义,"
                    "EPUB 内没有任何脚注引用")
    elif md_footnotes and epub_footnotes < md_footnotes:
        result.warn("content_footnotes_shrunk",
                    f"脚注数量减少: Markdown {md_footnotes} 条 → EPUB {epub_footnotes} 处引用")

    # ---- 链接:链接全丢才报(产物里的链接有 pandoc 自动生成的脚注回链,数量不可直接比) ----
    if md_links and epub_links == 0:
        result.warn("content_links_lost",
                    f"链接全部丢失: Markdown 有 {md_links} 个链接,EPUB 内没有任何 <a href>")

    # ---- 页覆盖:只有 Markdown 带页码注释时才有意义(hybrid / 扫描分片) ----
    if expected_pages and pages and pages < expected_pages * PAGE_COVER_RATIO:
        result.warn("content_page_coverage",
                    f"页覆盖不足: 原始 PDF {expected_pages} 页,Markdown 页码注释只覆盖 {pages} 页")

    return result
