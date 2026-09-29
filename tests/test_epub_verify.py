"""EPUB 结构校验(计划 1.1):先写失败用例,再抽 src/epub/verify.py。

覆盖:
  - 正常 EPUB 无 error
  - 图片被删 → image_missing(消息含「图片引用缺失」)
  - 期望公式/脚注/图片数量不满足 → 对应 error
  - zip 损坏 / 容器缺失 → readable=False 且给出归档级 error
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

from conftest import build_epub, corrupt_zip_member, rewrite_epub, truncate_file

from epub.verify import archive_issue, verify_epub


def test_valid_epub_has_no_errors(sample_epub: Path) -> None:
    r = verify_epub(sample_epub, expect_math=True, expect_images=1)
    assert r.readable
    assert r.errors == [], [i.message for i in r.errors]
    assert r.stats["math"] >= 1
    assert r.stats["img_refs"] >= 1
    assert r.stats["css"] >= 1
    assert r.stats["toc_links"] >= 1
    assert r.summary() == "0 失败 0 警告" or "0 失败" in r.summary()


def test_missing_image_is_reported(sample_epub: Path, tmp_path: Path) -> None:
    # 删掉包内图片(引用仍在),模拟「图片没被打进 EPUB」
    broken = rewrite_epub(sample_epub, tmp_path / "broken.epub", drop_suffixes=(".png", ".jpg"))
    r = verify_epub(broken)
    codes = [i.code for i in r.errors]
    assert "image_missing" in codes
    assert any("图片引用缺失" in i.message for i in r.errors)


def test_manifest_missing_file_is_reported(sample_epub: Path, tmp_path: Path) -> None:
    """manifest 列出但包内不存在 → manifest_missing(硬失败)。"""
    broken = rewrite_epub(sample_epub, tmp_path / "no_media.epub", drop_suffixes=(".png",))
    r = verify_epub(broken)
    assert any(i.code in ("manifest_missing", "image_missing") for i in r.errors)


def test_expect_math_fails_when_absent(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "w", tmp_path / "nofx.epub", with_math=False, with_image=False)
    assert verify_epub(epub).ok
    r = verify_epub(epub, expect_math=True)
    assert [i.code for i in r.errors] == ["math_expected"]


def test_expect_images_fails_when_fewer(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "w", tmp_path / "noimg.epub", with_image=False, with_math=False)
    r = verify_epub(epub, expect_images=2)
    assert "images_expected" in [i.code for i in r.errors]


def test_broken_zip_is_unreadable(tmp_path: Path) -> None:
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"this is not a zip file")
    r = verify_epub(bad)
    assert r.readable is False
    assert r.ok is False
    assert r.errors[0].code == "archive_unreadable"


def test_container_missing(tmp_path: Path, sample_epub: Path) -> None:
    broken = rewrite_epub(sample_epub, tmp_path / "nocontainer.epub",
                          drop={"META-INF/container.xml"})
    r = verify_epub(broken)
    assert "container_missing" in [i.code for i in r.errors]


def test_css_missing(tmp_path: Path) -> None:
    epub = build_epub(tmp_path / "w", tmp_path / "nocss.epub", with_image=False, with_math=False)
    broken = rewrite_epub(epub, tmp_path / "nocss2.epub", drop_suffixes=(".css",))
    r = verify_epub(broken)
    assert "css_missing" in [i.code for i in r.errors]


def test_stats_counts_math(sample_epub: Path) -> None:
    with zipfile.ZipFile(sample_epub) as zf:
        names = zf.namelist()
    r = verify_epub(sample_epub)
    assert r.stats["entries"] == len(names)
    assert r.stats["chapters"] >= 1


# ------------------------------------------------- 容器完好性(完成状态判断用)

def test_archive_issue_accepts_intact_epub(sample_epub: Path) -> None:
    assert archive_issue(sample_epub) is None


def test_archive_issue_detects_truncated_file(sample_epub: Path, tmp_path: Path) -> None:
    """截断的 EPUB(写到一半)必须被判为不完整 —— 它不是「成功产物」。"""
    cut = tmp_path / "cut.epub"
    cut.write_bytes(sample_epub.read_bytes())
    truncate_file(cut)                       # 只留前 30%
    issue = archive_issue(cut)
    assert issue is not None and "zip" in issue


def test_archive_issue_detects_empty_and_non_zip(tmp_path: Path) -> None:
    empty = tmp_path / "empty.epub"
    empty.write_bytes(b"")
    garbage = tmp_path / "garbage.epub"
    garbage.write_bytes(b"<html>502 Bad Gateway</html>")
    assert archive_issue(empty) is not None
    assert archive_issue(garbage) is not None


def test_archive_issue_detects_corrupt_member(sample_epub: Path, tmp_path: Path) -> None:
    """条目表完整但数据区坏了(大小/mtime 都正常)→ 仍必须判为不完整。"""
    copy = tmp_path / "corrupt.epub"
    copy.write_bytes(sample_epub.read_bytes())
    corrupt_zip_member(copy)
    issue = archive_issue(copy)
    # 数据区损坏可能表现为 CRC 校验失败,也可能在解压时直接炸(两种都算「不完整」)
    assert issue is not None and "损坏" in issue


def test_archive_issue_detects_missing_container(sample_epub: Path, tmp_path: Path) -> None:
    """空 ZIP(能打开、是合法 zip,但根本不是 EPUB)→ 不算完成。"""
    broken = rewrite_epub(sample_epub, tmp_path / "nocontainer.epub",
                          drop={"META-INF/container.xml"})
    issue = archive_issue(broken)
    assert issue is not None and "container.xml" in issue


# ------------------------------------------------- 结构校验:不良构 XHTML / spine

def _opf_of(epub: Path) -> str:
    with zipfile.ZipFile(epub) as zf:
        return zf.read("EPUB/content.opf").decode("utf-8")


def test_bare_br_makes_xhtml_invalid(sample_epub: Path, tmp_path: Path) -> None:
    """一个 `<br>` 就足以让整章不再是良构 XML(实测踩过,300KB 的章节整份失效)。"""
    broken = rewrite_epub(sample_epub, tmp_path / "badhtml.epub",
                          replace=(("</body>", "<br></body>"),))

    result = verify_epub(broken)

    assert any(i.code == "xhtml_invalid" for i in result.errors)
    assert not result.ok


def test_unclosed_tag_makes_xhtml_invalid(sample_epub: Path, tmp_path: Path) -> None:
    broken = rewrite_epub(sample_epub, tmp_path / "unclosed.epub",
                          replace=(("</p>", ""),))

    result = verify_epub(broken)

    assert any(i.code == "xhtml_invalid" for i in result.errors)


def test_spine_referencing_unknown_id_is_an_error(sample_epub: Path, tmp_path: Path) -> None:
    """spine 指向 manifest 里不存在的条目 → 该章会从阅读顺序里消失(以前静默丢弃)。"""
    opf = _opf_of(sample_epub)
    itemref = re.search(r'<itemref[^>]*idref="ch001_xhtml"[^>]*/>', opf)
    assert itemref, "样本结构变了:找不到 ch001 的 spine 项"
    broken = rewrite_epub(sample_epub, tmp_path / "badspine.epub",
                          replace=((itemref.group(0),
                                    '<itemref idref="nosuchitem" />'),))

    result = verify_epub(broken)

    assert any(i.code == "spine_broken" for i in result.errors)


def test_empty_spine_is_an_error(sample_epub: Path, tmp_path: Path) -> None:
    opf = _opf_of(sample_epub)
    spine = re.search(r"<spine.*?</spine>", opf, re.S)
    assert spine, "样本结构变了:找不到 spine"
    broken = rewrite_epub(sample_epub, tmp_path / "nospine.epub",
                          replace=((spine.group(0),
                                    re.sub(r"<itemref[^>]*/>", "", spine.group(0))),))

    result = verify_epub(broken)

    assert any(i.code == "spine_empty" for i in result.errors)


# ------------------------------------------------- 结构校验:TOC / nav / ncx

def test_broken_toc_link_is_an_error(sample_epub: Path, tmp_path: Path) -> None:
    """目录链接指向包内不存在的文件 → 读者点进去是空白页(以前只数条数,不看目标)。"""
    broken = rewrite_epub(sample_epub, tmp_path / "badtoc.epub",
                          replace=(('href="text/ch001.xhtml#', 'href="text/gone.xhtml#'),))

    result = verify_epub(broken)

    assert any(i.code == "toc_link_broken" for i in result.errors)
    assert result.stats["toc_links"] >= 1


def test_broken_ncx_link_is_an_error(sample_epub: Path, tmp_path: Path) -> None:
    """EPUB2 / 旧 Kindle 用 toc.ncx 做目录,它的目标同样必须存在。"""
    broken = rewrite_epub(sample_epub, tmp_path / "badncx.epub",
                          replace=(('src="text/ch001.xhtml#', 'src="text/gone.xhtml#'),))

    result = verify_epub(broken)

    assert any(i.code == "ncx_link_broken" for i in result.errors)


def test_missing_toc_warns(tmp_path: Path) -> None:
    """nav 里没有 toc 段(有正文标题却给不出目录)→ 告警,并且不当成链接断裂。"""
    src = tmp_path / "src"
    body = "".join(f"## 第 {i} 节\n\n正文段落,句号结尾。\n\n" for i in range(1, 7))
    epub = build_epub(src, tmp_path / "toc.epub", title="样本书", body=body,
                      with_image=False, with_math=False)
    with zipfile.ZipFile(epub) as zf:
        nav = zf.read("EPUB/nav.xhtml").decode("utf-8")      # 去掉 toc 段,只留 landmarks
    stripped = re.sub(r'<nav[^>]*epub:type="toc"[^>]*>.*?</nav>', "", nav, flags=re.S)
    assert stripped != nav
    broken = rewrite_epub(epub, tmp_path / "notoc.epub", replace=((nav, stripped),))

    result = verify_epub(broken)

    assert result.stats["headings"] >= 6
    assert any(i.code == "nav_not_toc" for i in result.warnings)
    assert any(i.code == "toc_empty" for i in result.warnings)
    assert not any(i.code == "toc_link_broken" for i in result.errors)


def test_nav_landmarks_do_not_inflate_toc_count(tmp_path: Path) -> None:
    """nav 里的 landmarks(Cover / Title Page / Table of Contents)不是目录条目。"""
    src = tmp_path / "src"
    body = "## 甲\n\n正文,句号结尾。\n\n## 乙\n\n正文,句号结尾。\n"
    epub = build_epub(src, tmp_path / "nav.epub", title="样本书", body=body,
                      with_image=False, with_math=False)
    with zipfile.ZipFile(epub) as zf:
        nav = zf.read("EPUB/nav.xhtml").decode("utf-8")
    assert 'epub:type="landmarks"' in nav and "Title Page" in nav

    result = verify_epub(epub)

    # 目录条目 = 正文标题(书名 + 两节);landmarks 的条目必须被排除在外
    landmarks = re.search(r'<nav[^>]*epub:type="landmarks"[^>]*>.*?</nav>', nav, re.S).group(0)
    landmark_links = len(re.findall(r"<a [^>]*href=", landmarks))
    assert landmark_links >= 2            # Title Page + Table of Contents(样本无封面)
    assert result.stats["toc_links"] == 3
    assert len(re.findall(r"<a [^>]*href=", nav)) == result.stats["toc_links"] + landmark_links


# ------------------------------------------------- 结构校验:CSS

def test_unreferenced_css_is_an_error(sample_epub: Path, tmp_path: Path) -> None:
    """样式表在包里却没人链接它 → 整套排版实际没生效。"""
    broken = rewrite_epub(sample_epub, tmp_path / "unlinked.epub",
                          replace=(('rel="stylesheet"', 'rel="x-stylesheet"'),))

    result = verify_epub(broken)

    assert any(i.code == "css_unlinked" for i in result.errors)


def test_css_link_to_missing_file_is_an_error(sample_epub: Path, tmp_path: Path) -> None:
    broken = rewrite_epub(sample_epub, tmp_path / "danglingcss.epub",
                          replace=(("styles/stylesheet1.css", "styles/gone.css"),))

    result = verify_epub(broken)

    assert any(i.code in ("css_link_broken", "manifest_missing") for i in result.errors)


# ------------------------------------------------- 结构校验:元数据格式

def test_invalid_language_and_date_warn(sample_epub: Path, tmp_path: Path) -> None:
    broken = rewrite_epub(sample_epub, tmp_path / "badmeta.epub",
                          replace=(("<dc:language>zh-CN</dc:language>",
                                    "<dc:language>zh CN 中文</dc:language>"),))

    result = verify_epub(broken)

    assert any(i.code == "language_invalid" for i in result.warnings)


# ------------------------------------------------- 结构校验:封面

def _cover_epub(tmp_path: Path, name: str = "cover") -> Path:
    """带封面的产物(与生产路径一致:`--epub-cover-image`)。"""
    import pymupdf

    work = tmp_path / f"{name}_work"
    work.mkdir(parents=True, exist_ok=True)
    artwork = work / "cover.jpg"
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 90))
    pix.set_rect(pix.irect, (30, 90, 160))
    pix.save(artwork)
    return build_epub(work / "src", work / f"{name}.epub", title="样本书",
                      extra_pandoc_args=["--epub-cover-image", str(artwork)])


def test_cover_is_referenced_and_first(tmp_path: Path) -> None:
    """正常封面产物零提示:封面图被封面页引用,且位于 spine 首位。"""
    epub = _cover_epub(tmp_path)

    result = verify_epub(epub)

    assert result.stats["cover"] == 1
    assert not any(i.code in ("cover_unreferenced", "cover_not_first") for i in result.issues)
    assert result.ok, [i.message for i in result.issues]


def test_cover_never_referenced_is_an_error(tmp_path: Path) -> None:
    """封面页被删掉(图还在 manifest)→ 打开就是空白封面页。"""
    epub = _cover_epub(tmp_path)
    broken = rewrite_epub(epub, tmp_path / "nocoverpage.epub",
                          drop={"EPUB/text/cover.xhtml"})

    result = verify_epub(broken)

    assert any(i.code in ("cover_unreferenced", "manifest_missing") for i in result.errors)


def test_cover_not_first_warns(tmp_path: Path) -> None:
    """封面页排到了 spine 中间 → 阅读器打开的第一页不是封面。"""
    epub = _cover_epub(tmp_path)
    opf = _opf_of(epub)
    spine = re.search(r"<spine.*?</spine>", opf, re.S).group(0)
    ref = re.search(r'<itemref[^>]*idref="cover_xhtml"[^>]*/>', spine).group(0)
    reordered = spine.replace(ref, "").replace("</spine>", f"  {ref}\n</spine>")
    broken = rewrite_epub(epub, tmp_path / "coverlast.epub",
                          replace=((spine, reordered),))

    result = verify_epub(broken)

    assert any(i.code == "cover_not_first" for i in result.warnings)


# ------------------------------------------------- 结构校验:新增计数

def test_stats_expose_structural_counts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    body = ("正文段落,含行尾硬换行  \n下一行仍在同一段。\n\n"
            "| 列一 | 列二 |\n|---|---|\n| a | b |\n\n"
            "```\nprint(1)\n```\n\n"
            "见 [链接](https://example.com)。\n")
    epub = build_epub(src, tmp_path / "counts.epub", title="样本书", body=body)

    stats = verify_epub(epub).stats

    assert stats["hard_breaks"] >= 1
    assert stats["tables"] == 1
    assert stats["code_blocks"] == 1
    # links 只算「内容里的链接」:目录 / 标题页里的导航链接不算(否则永远不为 0)
    assert stats["links"] == 1
    assert stats["links_internal"] >= 1
