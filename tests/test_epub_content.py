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

import batch
from conftest import build_epub, rewrite_epub, write_png
from epub.content import (
    markdown_code_block_count,
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
from epub.verify import verify_epub


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

def test_verify_output_merges_content_check_and_strict_raises(tmp_path: Path) -> None:
    """batch.verify_output 带上源 Markdown 后,--strict 必须因内容丢失而失败。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "few.epub", title="样本书", with_image=False)
    md = tmp_path / "book.md"
    md.write_text("# 样本书\n\n正文,句号结尾。\n\n![图](images/a.png)\n", encoding="utf-8")

    result = batch.verify_output(epub, book_md=md)          # 默认只告警
    assert any(i.code == "content_images_lost" for i in result.issues)
    assert not result.ok

    with pytest.raises(batch.VerifyError, match="图片丢失"):
        batch.verify_output(epub, book_md=md, strict=True)


def test_verify_output_without_source_stays_structure_only(tmp_path: Path) -> None:
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "ok.epub", title="样本书")

    result = batch.verify_output(epub)

    assert result.ok
    assert not any(i.code.startswith("content_") for i in result.issues)
