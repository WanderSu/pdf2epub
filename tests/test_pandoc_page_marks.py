"""Pandoc 输入侧的页码注释处理。

pandoc 把「第一个标题之前的内容」单独写成一个 xhtml 文件:只含一条 HTML 注释时,
成品首章是空的 —— 阅读器先翻到一页空白,结构校验还会报「疑似空章节」。页码注释
在阅读器里本来就不可见(HTML 注释),所以只删**首个标题之前**的那些;正文中间的
注释是页界标记,必须留着。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path


def test_leading_page_mark_is_dropped_for_pandoc() -> None:
    """第一个标题之前的页码注释不交给 pandoc。

    pandoc 把「第一个标题之前的内容」单独写成一个 xhtml:只含一条 HTML 注释时,
    成品首章就是空的(读者先看到空白页,校验报「疑似空章节」)。正文中间的注释
    是页界标记,必须留着。
    """
    from pdf2epub.epub.pandoc import drop_leading_page_marks

    leading = drop_leading_page_marks("<!-- page 1 -->\n# 第一章\n\n正文。\n")
    assert "<!-- page 1 -->" not in leading and "# 第一章" in leading

    middle = "# 第一章\n\n正文。\n\n<!-- page 2 -->\n\n正文二。\n"
    assert drop_leading_page_marks(middle) == middle


def test_book_starting_with_a_page_mark_has_no_empty_first_chapter(tmp_path: Path) -> None:
    """回归:带页码注释的电子书不能产出空的首章文件。"""
    import re

    from pdf2epub.epub.pandoc import build_epub

    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("<!-- page 1 -->\n# 第一章 起点\n\n正文段落,以句号结尾。\n",
                  encoding="utf-8")
    epub = build_epub(md, work, tmp_path / "out", title="某书", out_name="b",
                      title_page=False)

    with zipfile.ZipFile(epub) as z:
        for name in z.namelist():
            if not name.endswith(".xhtml") or name.endswith("nav.xhtml"):
                continue
            body = z.read(name).decode("utf-8").split("<body", 1)[1]
            visible = re.sub(r"<[^>]+>", "", re.sub(r"<!--.*?-->", "", body, flags=re.S))
            assert visible.strip(), f"{name} 是空章节"
