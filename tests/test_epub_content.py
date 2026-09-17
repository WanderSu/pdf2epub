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
    markdown_heading_count,
    markdown_images,
    markdown_math_count,
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
    """正常产物里标题页/目录天然短小,不能被算成空章节(否则每本书都在报警)。"""
    src = tmp_path / "src"
    epub = build_epub(src, tmp_path / "ok.epub", title="样本书")

    result = verify_epub(epub)

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
