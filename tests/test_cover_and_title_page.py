"""封面判定、cover.jpg 取图、标题页开关。

三件事必须一起钉住,它们共同决定读者打开书看到的第一屏:

1. ``detect_cover`` 能不能认出「这一页是封面」—— 认错的两个方向都有代价:
   把正文页当封面会拿一张正文图当缩略图;把封面当正文会多出一页重复的书名。
2. ``render_cover`` 取的是**原书封面图**(裁到主图矩形),不是整页栅格化 ——
   后者会把扫描仪黑边、纸张底色一起拍进去。
3. 有封面时**不生成** pandoc 标题页(``--epub-title-page=false``),没有封面时
   仍然生成 —— 两者的并集是"任何书都只有一处书名"。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from conftest import make_pdf, write_png


def _image_cover_pdf(path: Path, *, margin: int = 80) -> Path:
    """第 1 页整页是一张图(模拟扫描封面),第 2 页是正文。"""
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    img = write_png(path.parent / "cover_src.png", size=600)
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    rect = pymupdf.Rect(margin, margin, 595 - margin, 842 - margin)   # 留白 = 扫描黑边
    page.insert_image(rect, filename=str(img))
    body = doc.new_page(width=595, height=842)
    body.insert_textbox(pymupdf.Rect(60, 60, 535, 300),
                        "正文页:这一页有足够长的中文内容,用来保证检测器判定为文字页。"
                        "再补一句,保证有效字符数稳定超过阈值。",
                        fontname="china-s", fontsize=11, lineheight=1.6)
    doc.save(path)
    doc.close()
    return path


def test_detect_cover_image_dominant(tmp_path: Path) -> None:
    from cover import detect_cover

    info = detect_cover(_image_cover_pdf(tmp_path / "扫描封面.pdf"))
    assert info.is_cover is True
    assert info.image_ratio > 0.5
    assert info.clip is not None, "整页图应当给出裁切矩形"
    assert info.native_width > 0


def test_detect_cover_text_page_is_not_cover(tmp_path: Path) -> None:
    from cover import detect_cover

    info = detect_cover(make_pdf(tmp_path / "文字书.pdf", pages=2, with_image=False))
    assert info.is_cover is False
    assert info.text_chars > 80


def test_detect_cover_blank_page_is_not_cover(tmp_path: Path) -> None:
    import pymupdf

    from cover import detect_cover

    path = tmp_path / "空白.pdf"
    doc = pymupdf.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    doc.save(path)
    doc.close()

    info = detect_cover(path)
    assert info.is_cover is False, "空白页不是封面:拿它当 cover.jpg 只会得到一张白纸"
    assert info.reason == "空白页"


def test_detect_cover_missing_file_is_not_fatal(tmp_path: Path) -> None:
    from cover import detect_cover

    info = detect_cover(tmp_path / "不存在.pdf")
    assert info.is_cover is False, "封面判定失败不能阻断转换"


def test_render_cover_crops_to_the_image_rect(tmp_path: Path) -> None:
    """裁切矩形生效:封面比整页窄(留白被切掉),而不是整页宽度。"""
    import pymupdf

    from cover import detect_cover, render_cover

    pdf = _image_cover_pdf(tmp_path / "扫描封面.pdf", margin=80)
    info = detect_cover(pdf)
    out = render_cover(pdf, tmp_path / "cover.jpg", info=info)
    assert out is not None and out.exists()
    pix = pymupdf.Pixmap(str(out))
    # 整页渲染时宽高比 = 595/842 ≈ 0.707;裁掉 80pt 留白后 ≈ 435/682 ≈ 0.638
    assert pix.width / pix.height < 0.68, f"没有按主图裁切: {pix.width}x{pix.height}"


def test_build_epub_title_page_switch(tmp_path: Path) -> None:
    """``title_page=False`` 时产物里不能有 title_page.xhtml。"""
    from epub.pandoc import build_epub

    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("# 第一章 起点\n\n正文段落,以句号结尾。\n", encoding="utf-8")

    with_page = build_epub(md, work, tmp_path / "a", title="某书", out_name="a")
    without = build_epub(md, work, tmp_path / "b", title="某书", out_name="b",
                         title_page=False)

    def docs(p: Path) -> list[str]:
        with zipfile.ZipFile(p) as z:
            return [n for n in z.namelist() if n.endswith(".xhtml")]

    assert any("title_page" in n for n in docs(with_page))
    assert not any("title_page" in n for n in docs(without)), "关掉标题页后仍然生成了 title_page.xhtml"


def test_toc_depth_follows_heading_depth() -> None:
    from epub.pandoc import heading_depth

    assert heading_depth("# 一\n\n## 二\n") == 2
    assert heading_depth("没有标题\n") == 1
    # 代码块里的 # 不是标题
    assert heading_depth("# 一\n\n```\n### 注释\n```\n") == 1


@pytest.mark.parametrize("text, expected", [
    ("第一章 巴勒斯坦问题的由来", True),
    ("第 12 章 结论", True),
    ("Chapter 3", True),
    ("第十二部 上部", True),
    ("3. 方法论", True),
    ("后记", True),
    ("起源", False),
    ("一 历史的回顾", False),
])
def test_chapter_anchor(text: str, expected: bool) -> None:
    from clean.rules import is_chapter_anchor

    assert is_chapter_anchor(text) is expected


def test_structure_base_level_keeps_part_and_chapter() -> None:
    """部在外层、章在内层:基准取部级(不能把三十章全变成三十个文件)。"""
    from clean.rules import structure_base_level

    units = [(1, "第一部 上部"), (2, "第一章 一"), (2, "第二章 二"),
             (1, "第二部 下部"), (2, "第三章 三")]
    assert structure_base_level(units) == (1, "按部/卷判定基准 1")


def test_structure_base_level_refuses_when_a_title_is_above() -> None:
    """书名还在第 1 级时不做归一:平移会让书名与章同级,反而制造重复标题页。"""
    from clean.rules import structure_base_level

    units = [(1, "某书名"), (2, "第一章 一"), (2, "第二章 二")]
    base, why = structure_base_level(units)
    assert base is None and "书名" in why
