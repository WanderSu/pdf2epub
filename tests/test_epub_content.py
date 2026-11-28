"""EPUB 内容完整性校验测试(v0.3.2 P0-3)。

`verify_epub` 管「包合法」,`verify_content` 管「内容没丢」—— 拿产物和源 Markdown
对照。测试的原则与实现一致:**正常产物不许有任何提示**(否则用户会学会忽略提示),
明显的数量级差异必须报出来。

样本用真实工具链生成(pandoc + 项目 CSS),损坏/丢失样本用 conftest.rewrite_epub
做「替换 / 加条目」,或在生成 EPUB 时换掉 body 来模拟链路丢内容。
"""
from __future__ import annotations

from pathlib import Path

import pytest

import pdf2epub.batch as batch
from conftest import build_epub, rewrite_epub, write_png
from pdf2epub.epub.content import (    markdown_code_block_count,
    markdown_footnote_defs,
    markdown_hard_breaks,
    markdown_heading_count,
    markdown_images,
    markdown_link_count,
    markdown_math_count,
    markdown_table_count,
    markdown_text_chars,
    verify_content,
)
from pdf2epub.epub.verify import verify_epub

# ---------------------------------------------------------------- 正常产物 = 无提示

def test_matching_source_reports_no_content_issue(tmp_path: Path) -> None:
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "ok.epub", title="样本书")

    report = verify_content(src / "book.md", epub)

    assert report.issues == [], [i.message for i in report.issues]
    stats = report.stats
    assert stats["content_md_images"] == stats["content_epub_images"] >= 1
    assert stats["content_epub_math"] >= 1
    assert stats["content_epub_chars"] >= stats["content_md_chars"] * 0.4


def test_markdown_parsers() -> None:
    md = ("# 标题一\n\n正文段落,含 ![图](images/a.png) 与 ![图](images/b.png)。\n\n"
          "行内 $x^2$ 与行间:\n\n$$\n\\int_0^1 x dx\n$$\n\n## 标题二\n")
    assert markdown_images(md) == {"a.png", "b.png"}
    assert markdown_math_count(md) == 2
    assert markdown_heading_count(md) == 2
    assert markdown_text_chars(md) > 10


def test_markdown_structure_counters() -> None:
    """分行 / 表格 / 代码 / 脚注 / 链接的源侧计数(内容对照的分子)。"""
    md = ("# 书\n\n第一行  \n第二行(行尾两空格 = 硬换行)\n\n"
          "| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n\n"
          "```\ncode\n```\n\n"
          "正文末尾有注释[^1]。\n\n[^1]: 脚注内容。\n\n"
          "见 [链接](https://example.com)。\n")

    assert markdown_hard_breaks(md) == 1
    assert markdown_table_count(md) == 1
    assert markdown_code_block_count(md) == 1
    assert markdown_footnote_defs(md) == 1
    assert markdown_link_count(md) == 1


# ---------------------------------------------------------------- HTML 图片引用
#
# 云端 OCR(MinerU / PaddleOCR-VL)的图片框是原始 HTML:`<img src="images/x.jpg">`。
# 只数 `![]()` 会让内容对照在图片这一项上两头都错:正常书报「EPUB 比源多」
# (实测《夜行货车》2 张 vs 0 张),真丢图时又因为源侧也是 0 而永远不报错。


def test_markdown_images_counts_html_img() -> None:
    md = ('<div style="text-align:center;"><img src="images/a.jpg" alt="Image" /></div>\n\n'
          "<img src='images/b.jpg'>\n\n"
          "<img src=images/c.jpg>\n\n"
          "![普通图](images/d.png)\n")
    assert markdown_images(md) == {"a.jpg", "b.jpg", "c.jpg", "d.png"}


def test_html_only_images_do_not_report_extra(tmp_path: Path) -> None:
    """HTML 形态引用的图与产物一致时,不许报 `content_images_extra`。"""
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    write_png(src / "fig.png")
    body = ('正文段落,句号结尾。\n\n'
            '<div style="text-align:center;"><img src="fig.png" alt="Image" /></div>')
    epub = build_epub(src, tmp_path / "html-img.epub", title="样本书",
                      body=body, with_image=False)

    report = verify_content(src / "book.md", epub)

    codes = {i.code for i in report.issues}
    assert "content_images_extra" not in codes, [i.message for i in report.issues]
    assert "content_images_lost" not in codes, [i.message for i in report.issues]
    assert report.stats["content_md_images"] == report.stats["content_epub_images"] == 1


def test_lost_html_images_are_an_error(tmp_path: Path) -> None:
    """源 Markdown 用 HTML 引用 3 张图、产物里 0 张 → 仍然要报内容丢失。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "few.epub", title="样本书", with_image=False)
    md = tmp_path / "book.md"
    md.write_text("""# 样本书

正文段落,句号结尾。

<div><img src="images/a.png" /></div>

<div><img src="images/b.png" /></div>

<div><img src="images/c.png" /></div>
""", encoding="utf-8")

    report = verify_content(md, epub)

    assert any(i.code == "content_images_lost" for i in report.errors)
    assert report.stats["content_md_images"] == 3
    assert report.stats["content_epub_images"] == 0


def test_counters_ignore_code_blocks() -> None:
    """代码块里的 `  ` / `|---|` / `[^1]:` 都是示例文本,不能被当成结构。"""
    md = "```\n第一行  \n| a | b |\n|---|---|\n[^1]: x\n```\n"

    assert markdown_hard_breaks(md) == 0
    assert markdown_table_count(md) == 0
    assert markdown_footnote_defs(md) == 0
    assert markdown_code_block_count(md) == 1


# ---------------------------------------------------------------- 内容丢失(必须报错)

def test_lost_images_are_an_error(tmp_path: Path) -> None:
    """EPUB 里图片比源 Markdown 少 → 内容丢失(结构可能完全合法)。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "few.epub", title="样本书", with_image=False)
    md = tmp_path / "book.md"
    md.write_text("# 样本书\n\n正文段落,句号结尾。\n\n"
                  "![一](images/a.png)\n\n![二](images/b.png)\n\n![三](images/c.png)\n",
                  encoding="utf-8")

    report = verify_content(md, epub)

    assert any(i.code == "content_images_lost" for i in report.errors)
    assert report.stats["content_md_images"] == 3
    assert report.stats["content_epub_images"] == 0


def test_lost_math_is_an_error(tmp_path: Path) -> None:
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "nomath.epub", title="样本书", with_math=False)
    md = src / "book.md"
    md.write_text(md.read_text(encoding="utf-8") + "\n\n$$E = mc^2$$\n", encoding="utf-8")

    report = verify_content(md, epub)

    assert any(i.code == "content_math_lost" for i in report.errors)


def test_shrunk_text_is_an_error(tmp_path: Path) -> None:
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "short.epub", title="样本书",
                      body="只有一句话。", with_image=False, with_math=False)
    md = src / "book.md"
    md.write_text("# 样本书\n\n" + "这是一段很长的正文,重复很多次以保证字符数远超产物。" * 20 + "\n",
                  encoding="utf-8")

    report = verify_content(md, epub)

    assert any(i.code == "content_text_shrunk" for i in report.errors)


def test_page_coverage_below_expectation_warns(tmp_path: Path) -> None:
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "ok.epub", title="样本书")
    md = src / "book.md"
    md.write_text("# 样本书\n\n<!-- page 1 -->\n正文,句号结尾。\n\n"
                  "<!-- page 2-3 ocr -->\nOCR 正文,句号结尾。\n", encoding="utf-8")

    report = verify_content(md, epub, expected_pages=10)

    assert any(i.code == "content_page_coverage" for i in report.warnings)
    assert report.stats["content_covered_pages"] == 3
    # 没给 expected_pages 就不该报(文本版 Markdown 没有页码注释属正常)
    assert not any(i.code == "content_page_coverage"
                   for i in verify_content(md, epub).warnings)


# ---------------------------------------------------------------- 结构侧的内容信号

def test_poetry_hard_breaks_survive_into_the_epub(tmp_path: Path) -> None:
    """诗行靠 Markdown 硬换行才在阅读器里真的分行(`<br>`),不能被压成一行。"""
    src = tmp_path / "src"
    verse = ("## 静夜思\n\n床前明月光  \n疑是地上霜  \n举头望明月  \n低头思故乡\n")
    epub = build_epub(src, tmp_path / "verse.epub", title="诗选", body=verse,
                      with_image=False, with_math=False)

    report = verify_content(src / "book.md", epub)

    # 前 3 行带硬换行,第 4 行是段末(段末的行尾空格 pandoc 不渲染成 <br>)
    assert report.stats["content_md_breaks"] == 3
    assert report.stats["content_epub_breaks"] == 3
    assert report.issues == [], [i.message for i in report.issues]


def test_trailing_break_at_paragraph_end_is_not_counted() -> None:
    """段末的行尾空格不产生 `<br>`,源侧也不能把它算成硬换行(否则平白告警)。"""
    assert markdown_hard_breaks("只有一行  \n\n下一段\n") == 0
    assert markdown_hard_breaks("第一行  \n第二行\n\n下一段\n") == 1


def test_flattened_poetry_is_reported(tmp_path: Path) -> None:
    """源 Markdown 有硬换行、产物里一处都没有 → 诗被压成一行,必须报出来。"""
    src = tmp_path / "src"
    flat = "## 静夜思\n\n床前明月光\n疑是地上霜\n举头望明月\n低头思故乡\n"
    epub = build_epub(src, tmp_path / "flat.epub", title="诗选", body=flat,
                      with_image=False, with_math=False)
    md = tmp_path / "verse.md"
    md.write_text("# 诗选\n\n" + flat.replace("光\n", "光  \n"), encoding="utf-8")

    report = verify_content(md, epub)

    assert any(i.code == "content_breaks_lost" for i in report.warnings)
    assert report.stats["content_epub_breaks"] == 0


def test_lost_tables_and_code_are_reported(tmp_path: Path) -> None:
    """表格 / 代码块整批没落进产物 → 判 error(结构上看不出来,内容却没了)。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "plain.epub", title="样本书",
                      body="只有正文,句号结尾。", with_image=False, with_math=False)
    md = tmp_path / "rich.md"
    md.write_text("# 样本书\n\n正文,句号结尾。\n\n"
                  "| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n\n"
                  "```\nprint(1)\n```\n", encoding="utf-8")

    report = verify_content(md, epub)
    codes = [i.code for i in report.errors]

    assert "content_tables_lost" in codes
    assert "content_code_lost" in codes


def test_table_and_code_survive_into_the_epub(tmp_path: Path) -> None:
    src = tmp_path / "src"
    body = ("| 甲 | 乙 |\n|---|---|\n| 1 | 2 |\n\n```\nprint(1)\n```\n")
    epub = build_epub(src, tmp_path / "rich.epub", title="样本书", body=body,
                      with_image=False, with_math=False)

    report = verify_content(src / "book.md", epub)

    assert report.stats["content_md_tables"] == report.stats["content_epub_tables"] == 1
    assert report.stats["content_md_code"] == report.stats["content_epub_code"] == 1
    assert report.issues == [], [i.message for i in report.issues]


def test_lost_footnotes_are_reported(tmp_path: Path) -> None:
    """脚注定义在源里有、产物里一处脚注都没有 = 整批注释消失。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "nofn.epub", title="样本书",
                      body="正文,句号结尾。", with_image=False, with_math=False)
    md = tmp_path / "fn.md"
    md.write_text("# 样本书\n\n正文末尾有注释[^1]。\n\n[^1]: 脚注内容。\n", encoding="utf-8")

    report = verify_content(md, epub)

    assert any(i.code == "content_footnotes_lost" for i in report.errors)


def test_footnotes_survive_into_the_epub(tmp_path: Path) -> None:
    src = tmp_path / "src"
    body = "正文末尾有注释[^1]。\n\n[^1]: 脚注内容,说明来龙去脉。\n"
    epub = build_epub(src, tmp_path / "fn.epub", title="样本书", body=body,
                      with_image=False, with_math=False)

    report = verify_content(src / "book.md", epub)

    assert report.stats["content_md_footnotes"] == 1
    assert report.stats["content_epub_footnotes"] >= 1
    assert report.issues == [], [i.message for i in report.issues]
    # 脚注区块与引用都要在(阅读器要能点进去)
    assert verify_epub(epub).stats["footnote_sections"] >= 1


def test_cleaned_footnotes_survive_into_the_epub(tmp_path: Path) -> None:
    """清理器 → Pandoc → EPUB 全链路:脚注必须仍是 **EPUB 原生脚注**。

    样本刻意取最容易出事的形状:正文行不以句末标点结尾、脚注定义紧跟其后。
    修复前 `join_lines` 会把定义拼进正文段 —— 清理后源里连 `[^1]:` 都没有了,
    内容对照校验看不见,只有在这里(产物侧)才能发现整批注释降级成正文文字。
    """
    import re
    import zipfile

    from pdf2epub.clean import clean_text
    from pdf2epub.clean.options import CleanOptions
    from pdf2epub.epub.pandoc import build_epub as pandoc_build_epub
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)
    raw = ("# 脚注样本\n\n"
           "这种方法的效果在实验中得到了验证,而且重复了三次以上[^1]\n\n"
           "[^1]: 第一条脚注,内容是中文,长度足够长\n"
           "[^2]: 第二条脚注,内容也是中文,同样足够长\n\n"
           "结论段落正文正文正文正文正文[^2],以句号结尾。\n")
    cleaned = clean_text(raw, source="footnote.md", options=CleanOptions()).md_after
    assert markdown_footnote_defs(cleaned) == 2          # 定义没被吃掉

    book_md = src / "book.md"
    book_md.write_text(cleaned, encoding="utf-8")
    # 走生产路径(pandoc 副本归一化 + 项目 CSS + 元数据),不用测试夹具拼 md
    epub = pandoc_build_epub(book_md, src, tmp_path / "output",
                             title="脚注样本", out_name="脚注样本")

    report = verify_content(book_md, epub)
    assert report.issues == [], [i.message for i in report.issues]
    assert report.stats["content_md_footnotes"] == 2
    assert report.stats["content_epub_footnotes"] == 2

    with zipfile.ZipFile(epub) as zf:
        xhtml = " ".join(zf.read(n).decode("utf-8") for n in zf.namelist()
                         if n.endswith(".xhtml"))
    assert len(re.findall(r'epub:type="footnote"', xhtml)) == 2
    assert "第一条脚注" in xhtml and "第二条脚注" in xhtml


def test_lost_links_warn(tmp_path: Path) -> None:
    """链接全丢只告警(产物里的链接含 pandoc 自动生成的脚注回链,数量不可直接比)。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "nolink.epub", title="样本书",
                      body="只有正文,句号结尾。", with_image=False, with_math=False)
    md = tmp_path / "link.md"
    md.write_text("# 样本书\n\n见 [官方文档](https://example.com)。\n", encoding="utf-8")

    report = verify_content(md, epub)

    assert any(i.code == "content_links_lost" for i in report.warnings)


def test_ocr_picture_text_break_is_counted(tmp_path: Path) -> None:
    """云端 OCR 的图片文字块用原始 HTML `<br>` 分行,同样计入硬换行。"""
    src = tmp_path / "src"
    body = "<!-- Start of picture text -->\n出版社<br>版权所有\n"
    epub = build_epub(src, tmp_path / "br.epub", title="样本书", body=body,
                      with_image=False, with_math=False)

    md = tmp_path / "book.md"
    md.write_text(f"# 样本书\n\n{body}", encoding="utf-8")

    report = verify_content(md, epub)

    assert report.stats["content_md_breaks"] == 1
    assert report.stats["content_epub_breaks"] >= 1
    assert not any(i.code == "content_breaks_lost" for i in report.issues)


# ---------------------------------------------------------------- 结构侧的内容信号

def test_mass_empty_chapters_is_an_error(tmp_path: Path) -> None:
    """整本书都只剩标题(正文全空)→ 内容疑似丢失,判 error 而不是 warning。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "empty.epub", title="样本书",
                      body="# 空章一\n\n# 空章二\n\n# 空章三\n", with_image=False, with_math=False)

    result = verify_epub(epub)

    assert any(i.code == "chapters_empty_mass" for i in result.errors)
    assert result.stats["empty_chapters"] >= 3
    assert not result.ok


def test_title_page_and_nav_do_not_count_as_empty_chapters(tmp_path: Path) -> None:
    """正常产物里标题页/目录/标题章天然短小,不能被算成空章节。

    三类都不算:标题页与目录(epub:type=frontmatter/titlepage/toc)、封面页
    (只有 `<body id="cover">`)、pandoc 为 Markdown 首个 h1 单独生成的**标题章**
    (它只有书名、没有正文)。漏掉任何一个,每本正常书都会带一条无用告警。
    """
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "ok.epub", title="样本书")

    result = verify_epub(epub)

    assert result.stats["chapters"] >= 3          # 标题页 + 目录 + 正文章
    assert result.stats["empty_chapters"] == 0
    assert not any(i.code == "empty_chapters" for i in result.issues)
    assert result.ok


def test_orphan_image_warns(tmp_path: Path) -> None:
    """manifest 里有、正文从不引用的图片 → 只告警(可能是封面/装饰)。"""
    src = tmp_path / "src"
    good = build_epub(src, tmp_path / "ok.epub", title="样本书")
    write_png(tmp_path / "orphan.png")
    epub = rewrite_epub(
        good, tmp_path / "orphan.epub",
        add={"EPUB/media/orphan.png": (tmp_path / "orphan.png").read_bytes()},
        replace=(("</manifest>",
                  '<item id="orphan" href="media/orphan.png" media-type="image/png"/></manifest>'),),
    )

    result = verify_epub(epub)

    assert result.stats["orphan_images"] == 1
    assert any(i.code == "image_orphan" for i in result.warnings)
    assert result.ok          # 孤立图片不阻断(只是提示)


# ---------------------------------------------------------------- 与转换流程接线

def test_verify_output_merges_content_check_and_raises(tmp_path: Path) -> None:
    """batch.verify_output 带上源 Markdown 后,内容丢失(致命)必须判失败。

    内容对照是**结构校验发现不了**的那一类损坏:包完全合法,正文却少了几章、
    图片少了大半。它属于致命问题,默认模式(不只是 --strict)就要判失败。
    """
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "few.epub", title="样本书", with_image=False)
    md = tmp_path / "book.md"
    md.write_text("# 样本书\n\n正文,句号结尾。\n\n![图](images/a.png)\n", encoding="utf-8")

    with pytest.raises(batch.VerifyError, match="图片丢失"):
        batch.verify_output(epub, book_md=md)            # 默认模式:同样判失败
    with pytest.raises(batch.VerifyError, match="图片丢失"):
        batch.verify_output(epub, book_md=md, strict=True)


def test_verify_output_without_source_stays_structure_only(tmp_path: Path) -> None:
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "ok.epub", title="样本书")

    result = batch.verify_output(epub)

    assert result.ok
    assert not any(i.code.startswith("content_") for i in result.issues)


def test_markdown_math_count_ignores_footnote_markers() -> None:
    """行内公式计数必须按 pandoc 的定界规则:OCR 把脚注标记包成的 `$ ^{①} $` 不是公式。

    不按规则数,一本脚注多的书会被判「公式全部丢失」(标记被当成公式,而 EPUB 里
    当然没有对应的 MathML)—— 真实书(PaddleOCR,1957 年 8 页)上就踩到了。
    """
    md = ("正文末的注释标记 $ ^{⑧} $ 不是公式。\n\n"
          "行内公式 $E = mc^2$ 是公式。\n\n"
          "$$\n\\int_0^1 x dx\n$$\n")

    assert markdown_math_count(md) == 2


def test_markdown_math_count_counts_valid_inline_math() -> None:
    assert markdown_math_count("正文 $x^{2}$ 与 $a_1$ 两处。") == 2
    assert markdown_math_count("OCR 的空格形态 $ x $ 不算(pandoc 不认)。") == 0


# ---------------------------------------------------------------- 脚注回链(pandoc 不写)

def _epub_xhtml(path: Path) -> str:
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return "\n".join(zf.read(n).decode("utf-8", errors="replace")
                         for n in zf.namelist() if n.endswith(".xhtml"))


def test_footnote_backlinks_are_added_without_breaking_the_epub(tmp_path: Path) -> None:
    """产物侧补 doc-backlink(引用侧 pandoc 只写 noteref);补完容器仍合法。"""
    from pdf2epub.epub.pandoc import add_footnote_marks
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "fn.epub", title="样本书", with_image=False,
                      with_math=False, body="正文末尾有注释[^1]。\n\n[^1]: 脚注内容,足够长。\n")

    # 基线:conftest 的样本直接用 pandoc 生成,没有回链 —— pandoc 的 epub3 writer
    # 本来就不写 doc-backlink(与项目代码无关)
    assert "doc-backlink" not in _epub_xhtml(epub)

    assert add_footnote_marks(epub) == 1

    xhtml = _epub_xhtml(epub)
    assert 'href="#fnref1" class="footnote-back"' in xhtml
    assert 'role="doc-backlink"' in xhtml
    # 引用还在(补回链不能动正文侧),容器校验也照样通过
    assert 'href="#fn1"' in xhtml and "noteref" in xhtml
    assert verify_epub(epub).ok


def test_epub_without_footnotes_is_left_alone(tmp_path: Path) -> None:
    """没有脚注的书 → 连 zip 都不重写(字节不变,零风险)。"""
    from pdf2epub.epub.pandoc import add_footnote_marks
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "plain.epub", title="样本书")
    before = epub.read_bytes()

    assert add_footnote_marks(epub) == 0
    assert epub.read_bytes() == before


def test_duplicate_xhtml_ids_are_made_unique_without_moving_first_anchor(tmp_path: Path) -> None:
    """两个 OCR/Markdown 片段都叫 Footnotes 时，EPUB 的 XML id 仍必须唯一。"""
    import zipfile

    from pdf2epub.epub.pandoc import dedupe_xhtml_ids
    epub = tmp_path / "duplicate-id.epub"
    with zipfile.ZipFile(epub, "w") as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("OEBPS/ch.xhtml", (
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<section id="footnotes">one</section><section id="footnotes">two</section>'
            '<p id="footnotes-2">three</p></body></html>'
        ))

    assert dedupe_xhtml_ids(epub) == 1
    xhtml = _epub_xhtml(epub)
    assert xhtml.count('id="footnotes"') == 1
    assert 'id="footnotes-3"' in xhtml  # -2 已被占用，不能再制造冲突


def test_footnote_numbers_match_the_in_text_markers(tmp_path: Path) -> None:
    """脚注块的序号必须与正文标注一致;按章分文件时每章各自从 1 开始。

    回归:pandoc 的 `<aside>` 里**没有序号**,正文标着 1、2、3,书末却是几段没有
    编号的文字(读者对不上)。产物侧按文档内出现顺序补 `1.`、`2.`,与正文标注
    一一对应 —— 编号取位置而不是 `id="fnX"` 的数字(自定义标签时两者不相等)。
    """
    import re
    import zipfile

    from pdf2epub.epub.pandoc import build_epub
    md = tmp_path / "两章.md"
    md.write_text(
        "# 第一章\n\n第一章正文[^a],还有[^b]。\n\n[^a]: 甲注\n[^b]: 乙注\n\n"
        "# 第二章\n\n第二章正文[^c],还有[^d]。\n\n[^c]: 丙注\n[^d]: 丁注\n",
        encoding="utf-8")
    epub = build_epub(md, tmp_path / "work", tmp_path / "out",
                      title="两章", out_name="两章")

    with zipfile.ZipFile(epub) as zf:
        chapters = {n: zf.read(n).decode("utf-8") for n in zf.namelist()
                    if n.endswith(".xhtml") and "/ch" in n}

    assert len(chapters) == 2, sorted(chapters)
    for name, text in sorted(chapters.items()):
        refs = re.findall(r'class="footnote-ref"[^>]*>(\d+)<', text)
        nums = re.findall(r'<span class="footnote-num">(\d+)\.</span>', text)
        assert refs == ["1", "2"], name          # 每章从 1 开始(pandoc 的口径)
        assert nums == refs, f"{name}: 标注 {refs} ≠ 脚注序号 {nums}"


def test_backlink_injection_is_idempotent_and_keeps_mimetype_first(tmp_path: Path) -> None:
    """补两次不能补出两条回链;重写 zip 时 mimetype 仍必须是第一个且不压缩。"""
    import zipfile

    from pdf2epub.epub.pandoc import add_footnote_marks
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "fn.epub", title="样本书", with_image=False,
                      with_math=False, body="注释[^1]。\n\n[^1]: 脚注。\n")

    assert add_footnote_marks(epub) == 1
    assert add_footnote_marks(epub) == 0                    # 已有回链:不再重复补
    assert _epub_xhtml(epub).count("doc-backlink") == 1
    with zipfile.ZipFile(epub) as zf:
        first = zf.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED


def test_verify_content_reuses_caller_structure(tmp_path: Path, monkeypatch) -> None:
    """传入 structure= 时不得再跑一遍 verify_epub(一次转换只校验一次结构)。

    回归:verify_output 先 verify_epub,verify_content 内部又调一次 —— 300 章的
    书上这一遍约占校验耗时的 20%,结论却一字不差。
    """
    import pdf2epub.epub.content as content_mod
    from pdf2epub.epub.verify import VerifyResult
    src = tmp_path / "w"
    epub = build_epub(src, tmp_path / "ok.epub", with_image=False, with_math=False)
    md = src / "book.md"

    calls = []
    real_verify = content_mod.verify_epub
    monkeypatch.setattr(content_mod, "verify_epub",
                        lambda *a, **k: calls.append(a) or real_verify(*a, **k))
    verify_content(md, epub)                       # 不传:自己算(兼容旧调用)
    assert len(calls) == 1

    calls.clear()
    structure = VerifyResult()                     # 调用方已算好的结构结果
    structure.stats.update(real_verify(epub).stats)
    verify_content(md, epub, structure=structure)
    assert calls == []                             # 传了就不许再算


def test_verify_content_reuses_caller_structure(tmp_path: Path, monkeypatch) -> None:
    """传入 structure= 时不得再跑一遍 verify_epub(一次转换只校验一次结构)。

    回归:verify_output 先 verify_epub,verify_content 内部又调一次 —— 300 章的
    书上这一遍约占校验耗时的 20%,结论却一字不差。
    """
    import pdf2epub.epub.content as content_mod
    from pdf2epub.epub.verify import VerifyResult
    src = tmp_path / "w"
    epub = build_epub(src, tmp_path / "ok.epub", with_image=False, with_math=False)
    md = src / "book.md"

    calls = []
    real_verify = content_mod.verify_epub
    monkeypatch.setattr(content_mod, "verify_epub",
                        lambda *a, **k: calls.append(a) or real_verify(*a, **k))
    verify_content(md, epub)                       # 不传:自己算(兼容旧调用)
    assert len(calls) == 1

    calls.clear()
    structure = VerifyResult()                     # 调用方已算好的结构结果
    structure.stats.update(real_verify(epub).stats)
    verify_content(md, epub, structure=structure)
    assert calls == []                             # 传了就不许再算


def test_markdown_text_chars_ignores_void_html() -> None:
    """``<br>`` / ``<img>`` 在 Markdown 侧不产出可见文字,不能算进正文长度。

    修复前:``<br />`` 在源侧被数成 5 个字符,产物侧被整段剥掉 —— 一本靠 ``<br>``
    分行的 OCR 书源侧虚增 10% 以上,触发 ``content_text_less``(``--strict`` 下直接
    判失败),而正文一个字都没少。
    """
    from pdf2epub.epub.content import markdown_text_chars
    assert markdown_text_chars("一二<br>三四<br />五六") == 6
    assert markdown_text_chars('一二<img src="a.png">三四') == 4
    # 被 pandoc 转义成**文字**的尖括号片段仍然是正文,不能被当成标签剥掉
    assert markdown_text_chars("<repetitio>") == len("<repetitio")

# ---------------------------------------------------------------- 页级质量检查
# 整体字符数对得上,不代表每页都好:一页只识别出页码、一页整页漏字,总量可能仍有 96%。
# 这里逐页看文字量与图片数,把「空页」交给人复核;扫描书的图版页天然没字,只统计不告警。


def test_page_quality_classifies_blank_image_only_and_short_pages() -> None:
    from pdf2epub.epub.content import page_quality

    md = """\
<!-- page 1 -->
第一页正文内容足够长,长到不会被算成短页;这一页还有更多字用来充数,凑够五十个非空白字符,再补一些字确保超过阈值。

<!-- page 2 -->

<!-- page 3 -->
![图](images/a.png)

<!-- page 4 -->
短

<!-- page 5 -->
第五页正文内容足够长,长到不会被算成短页;这一页还有更多字用来充数,凑够五十个非空白字符,再补一些字确保超过阈值。
"""
    q = page_quality(md)
    assert q.pages == 5
    assert q.blank == (2,)
    assert q.image_only == (3,)
    assert q.short == (4,)


def test_page_quality_skips_merged_page_markers() -> None:
    from pdf2epub.epub.content import page_quality

    md = "<!-- page 2,4 ocr -->\n\n合并页内容\n\n<!-- page 5 -->\n\n第五页内容足够长一些。\n"
    q = page_quality(md)
    assert q.pages == 3          # 2、4、5
    assert q.blank == ()         # 合并页没法逐页判断,不能算空白


def test_many_blank_pages_raise_a_warning(tmp_path: Path) -> None:
    from conftest import build_epub
    from pdf2epub.epub.content import verify_content

    # 10 页里 4 页空白(阈值 max(3, 5% * 10) = 3)
    parts = []
    for page in range(1, 11):
        body = "" if page in (3, 5, 7, 9) else f"第 {page} 页正文,内容足够长一些。"
        parts.append(f"<!-- page {page} -->\n\n{body}")
    md = "\n\n".join(parts) + "\n"
    src = tmp_path / "src"
    src.mkdir()
    epub = build_epub(src, tmp_path / "book.epub", body="")
    # build_epub 会自己写一份 book.md:先造 EPUB,再覆盖成带页注释的源
    (src / "book.md").write_text(md, encoding="utf-8")
    result = verify_content(src / "book.md", epub, expected_pages=10)
    issues = [i for i in result.issues if i.code == "content_pages_blank"]
    assert issues and issues[0].level == "warning"
    assert "4/10" in issues[0].message
    assert result.stats["content_pages_blank"] == 4
