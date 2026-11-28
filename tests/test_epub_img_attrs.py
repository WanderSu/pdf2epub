"""原始 HTML 图片的 width/height:EPUB 3 只接受整数。

被钉住的缺陷:后端把版面原样抄成 ``<img alt="Image" width="52%">``,pandoc 原样
透传,EPUBCheck 对整本书报
``RSC-005: value of attribute "width" is invalid; must be an integer``(实测
《夜行货车》ch002)。百分比本身是合法 CSS,所以修法是把尺寸挪进 ``style``,而不是
把尺寸丢掉。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

from conftest import build_epub

from pdf2epub.backends.base import fix_img_size_attrs, normalize_image_refs
from pdf2epub.epub.verify import verify_epub


def test_percentage_width_moves_into_style() -> None:
    fixed = fix_img_size_attrs('<img alt="Image" width="52%" src="a.png">')
    assert 'style="width:52%"' in fixed
    assert 'width="52%"' not in fixed
    assert 'alt="Image"' in fixed and 'src="a.png"' in fixed


def test_self_closing_tag_keeps_the_slash() -> None:
    """``<img ... />`` 的样式必须插在 ``/>`` 之前,否则标签被打断、图片静默消失。"""
    fixed = fix_img_size_attrs('<img alt="Image" width="52%" src="a.png" />')
    assert fixed.endswith('style="width:52%" />')
    assert "/ style" not in fixed
    assert 'src="a.png"' in fixed


def test_integer_size_is_left_alone() -> None:
    tag = '<img src="a.png" width="120" height="80">'
    assert fix_img_size_attrs(tag) == tag


def test_mixed_sizes_merge_into_existing_style() -> None:
    fixed = fix_img_size_attrs('<img style="border:0" width="12em" height="80" src="a.png">')
    assert 'style="width:12em; border:0"' in fixed
    assert 'height="80"' in fixed and "width=" not in fixed.split("style=")[1]


def test_plain_markdown_images_are_untouched() -> None:
    md = "![图](images/a.png)\n\n普通段落。"
    assert fix_img_size_attrs(md) == md


def test_normalize_image_refs_also_fixes_sizes(tmp_path: Path) -> None:
    md = '<img src="images/x.png" width="52%">'
    fixed = normalize_image_refs(md, tmp_path / "images")
    assert 'style="width:52%"' in fixed and 'width="52%"' not in fixed


def _first_xhtml(epub: Path) -> str:
    with zipfile.ZipFile(epub) as zf:
        for name in sorted(zf.namelist()):
            if name.startswith("EPUB/text/") and name.endswith(".xhtml"):
                return zf.read(name).decode("utf-8", errors="replace")
    raise AssertionError("EPUB 里没有正文 xhtml")


def test_verifier_flags_invalid_size_attributes(tmp_path: Path) -> None:
    """兜底检查:漏网的百分比尺寸要出现在校验结果里(警告,不判失败)。"""
    body = '<div><img alt="Image" width="52%" src="img1.png"></div>'
    epub = build_epub(tmp_path / "src", tmp_path / "bad.epub", body=body)
    assert 'width="52%"' in _first_xhtml(epub)      # pandoc 原样透传
    result = verify_epub(epub)
    issues = [i for i in result.issues if i.code == "img_size_attribute"]
    assert issues and issues[0].level == "warning"
    assert "52%" in issues[0].message


def test_fixed_markdown_produces_no_size_warning(tmp_path: Path) -> None:
    body = fix_img_size_attrs('<div><img alt="Image" width="52%" src="img1.png"></div>')
    epub = build_epub(tmp_path / "src", tmp_path / "ok.epub", body=body)
    assert 'width="52%"' not in _first_xhtml(epub)
    result = verify_epub(epub)
    assert not [i for i in result.issues if i.code == "img_size_attribute"]
