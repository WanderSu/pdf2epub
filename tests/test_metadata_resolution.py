"""元数据分级(文件名 > PDF 元数据 > 卷首字段 > 首个 H1)与产物侧补丁。

盯住四件事:

1. **分级回落**:扫描仪起的名字(`扫描0001`)、PDF 元数据里的
   「Microsoft Word - 报告.doc」都不能当书名;文件名写清楚了就不许被别处覆盖。
2. **作者位只放作者本人**:扉页上的译者不进 `dc:creator`(阅读器会把译本显示成
   两个作者),改走 `dc:contributor role="trl"`。
3. **`--split-level=1` 显式传**:一章一个 XHTML 是「脚注落本章末尾」的前提,
   不能靠 pandoc 的默认值。
4. **CSS 认 `aside[epub|type~="footnote"]`**:pandoc 3.x 的实际产物形态。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import BOOK_CSS, PROJECT_ROOT, _pandoc, make_pdf
from metadata import (first_heading, front_fields, pdf_document_meta,
                      resolve_book_meta)

# ---------------------------------------------------------------- 分级


def test_filename_wins_when_it_looks_like_a_title() -> None:
    meta = resolve_book_meta(
        stem_title="巴勒斯坦问题", stem_author=None,
        pdf_meta={"title": "Microsoft Word - 巴勒斯坦问题.doc", "author": "Administrator"},
        front={"title": "另一个书名", "author": "另一个人"},
    )
    assert meta.title == "巴勒斯坦问题", "文件名写清楚了,就不许被 PDF 元数据或卷首改掉"
    assert meta.sources["title"] == "filename"
    assert meta.author == "另一个人", "PDF 元数据里的 Administrator 是垃圾值,跳过它继续往下找"
    assert meta.sources["author"] == "front_matter"


def test_junk_filename_falls_through_to_pdf_metadata() -> None:
    meta = resolve_book_meta(
        stem_title="扫描0001", pdf_meta={"title": "规训与惩罚", "author": "米歇尔·福柯"},
    )
    assert meta.title == "规训与惩罚"
    assert meta.sources["title"] == "pdf"
    assert meta.author == "米歇尔·福柯"
    assert meta.sources["author"] == "pdf"


def test_junk_pdf_title_falls_through_to_front_matter() -> None:
    meta = resolve_book_meta(
        stem_title="IMG_2043",
        pdf_meta={"title": "Microsoft Word - 未命名.doc"},
        front={"title": "乡土中国"},
    )
    assert meta.title == "乡土中国"
    assert meta.sources["title"] == "front_matter"


def test_heading_is_the_last_resort_and_chapter_names_do_not_count() -> None:
    meta = resolve_book_meta(stem_title="新建 DOC 文档", heading="第一章 起头")
    assert meta.title == "新建 DOC 文档", "章名不是书名,四级都没有就用回文件名"
    assert meta.sources["title"] == "filename"

    meta = resolve_book_meta(stem_title="新建 DOC 文档", heading="artificial intelligence")
    assert meta.title == "artificial intelligence"
    assert meta.sources["title"] == "heading"


def test_title_is_never_empty() -> None:
    meta = resolve_book_meta(stem_title="")
    assert meta.title == "未命名", "空 dc:title 会让书架出现一张无名卡片"


def test_author_falls_back_to_front_matter_and_strips_the_tail() -> None:
    meta = resolve_book_meta(
        stem_title="理想国", stem_author=None, front={"author": "柏拉图 著"},
    )
    assert meta.author == "柏拉图"
    assert meta.sources["author"] == "front_matter"


def test_author_stays_empty_when_every_source_is_junk() -> None:
    meta = resolve_book_meta(
        stem_title="书", stem_author="Author",
        pdf_meta={"author": "Administrator"}, front={"author": "User"},
    )
    assert meta.author is None, "宁可不写作者,也不要写 Administrator"


def test_publisher_comes_only_from_front_fields() -> None:
    meta = resolve_book_meta(stem_title="书", front={"publisher": "商务印书馆"})
    assert meta.publisher == "商务印书馆"
    assert meta.sources["publisher"] == "front_matter"
    # PDF 的 Producer/Brand 是排版软件,不是出版社 —— 故意不取
    assert resolve_book_meta(stem_title="书").publisher is None


# ---------------------------------------------------------------- 卷首字段


FRONT = """# 巴勒斯坦问题

作者：[美] 爱德华·萨义德 著
译者：金钥
出版社：生活·读书·新知三联书店

## 第一章 起头

这是一段正文,提到作者:某某某,但它在句中,不是字段。
"""


def test_front_fields_reads_labelled_lines() -> None:
    fields = front_fields(FRONT)
    assert fields["author"] == "[美] 爱德华·萨义德 著"
    assert fields["translator"] == "金钥"
    assert fields["publisher"] == "生活·读书·新知三联书店"


def test_front_fields_splits_two_fields_on_one_line() -> None:
    fields = front_fields("作者：柏拉图 译者：郭斌和\n")
    assert fields["author"] == "柏拉图"
    assert fields["translator"] == "郭斌和", "同一行两个字段:读到下一个标签就截断"


def test_front_fields_ignores_prose_that_mentions_the_words() -> None:
    prose = "本书作者认为,这个问题至今没有答案,而且短期内也不会有。\n"
    assert "author" not in front_fields(prose), "「作者认为…」是句子,不是字段"


def test_front_fields_ignores_sentence_like_values() -> None:
    assert "publisher" not in front_fields("出版社：这家出版社今年已经出了三十种新书了,据说还要再出。\n")


def test_first_heading_skips_structure_and_chapter_titles() -> None:
    assert first_heading("# 前言\n\n正文\n\n# 第一章 起头\n") is None
    assert first_heading("# 第一章 起头\n\n正文\n") is None
    assert first_heading("# 巴勒斯坦问题\n\n正文\n") == "巴勒斯坦问题"


def test_pdf_document_meta_reads_and_filters(tmp_path: Path) -> None:
    import pymupdf

    src = make_pdf(tmp_path / "原文.pdf", pages=1, with_image=False)
    good = tmp_path / "有元数据.pdf"
    doc = pymupdf.open(src)
    doc.set_metadata({"title": "规训与惩罚", "author": "米歇尔·福柯"})
    doc.save(good)
    doc.close()
    assert pdf_document_meta(good) == {"title": "规训与惩罚", "author": "米歇尔·福柯"}

    bad = tmp_path / "垃圾元数据.pdf"
    doc = pymupdf.open(src)
    doc.set_metadata({"title": "Microsoft Word - 报告.doc", "author": "Administrator"})
    doc.save(bad)
    doc.close()
    assert pdf_document_meta(bad) == {}, "垃圾元数据要当没有"

    assert pdf_document_meta(tmp_path / "不存在.pdf") == {}, "读不到不是失败理由"


# ---------------------------------------------------------------- 命令行


def _capture_cmd(monkeypatch, tmp_path: Path) -> list[list[str]]:
    """把 pandoc 换成记录参数的桩:不依赖本机 pandoc,也不产出真 EPUB。"""
    from epub import pandoc as pandoc_mod

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"stub")     # 骗过「产物非空」检查
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(pandoc_mod, "require_pandoc", lambda: "pandoc")
    monkeypatch.setattr(pandoc_mod, "_run_pandoc", fake_run)
    return calls


def test_split_level_is_passed_explicitly(monkeypatch, tmp_path: Path) -> None:
    from epub.pandoc import build_epub

    calls = _capture_cmd(monkeypatch, tmp_path)
    md = tmp_path / "book.md"
    md.write_text("# 第一章\n\n正文\n", encoding="utf-8")
    build_epub(md, tmp_path, tmp_path, title="书")

    cmd = calls[0]
    assert "--split-level=1" in cmd, "一章一个 XHTML 是脚注落本章末尾的前提,不能靠默认值"


def test_publisher_goes_through_pandoc_metadata(monkeypatch, tmp_path: Path) -> None:
    from epub.pandoc import build_epub

    calls = _capture_cmd(monkeypatch, tmp_path)
    md = tmp_path / "book.md"
    md.write_text("# 第一章\n\n正文\n", encoding="utf-8")
    build_epub(md, tmp_path, tmp_path, title="书", publisher="商务印书馆", translator="金钥")

    cmd = calls[0]
    assert "publisher=商务印书馆" in cmd
    # 译者**不**走 --metadata( pandoc 会静默丢掉 ),由产物 OPF 补 —— 见 add_contributor
    assert not any(a.startswith("translator=") for a in cmd)


# ---------------------------------------------------------------- 产物


def test_translator_lands_in_contributor_not_creator(tmp_path: Path) -> None:
    """端到端:卷首写着译者 → dc:contributor(trl),dc:creator 里只有作者本人。"""
    _pandoc()
    import batch

    src = tmp_path / "扫描0001.md"
    src.write_text(FRONT, encoding="utf-8")

    result = batch.process_batch(
        [src], config={}, work_root=tmp_path / "work", output_dir=tmp_path / "out", force=True,
    )[0]
    assert result.status == "done", result.error

    with zipfile.ZipFile(result.epub) as z:
        opf = z.read("EPUB/content.opf").decode("utf-8")
        css = "".join(
            z.read(n).decode("utf-8")
            for n in z.namelist() if n.endswith(".css")
        )

    title = re.search(r"<dc:title[^>]*>([^<]+)</dc:title>", opf).group(1)
    assert title == "巴勒斯坦问题", "文件名是扫描仪给的,书名应当从卷首字段来"

    creator = re.search(r"<dc:creator[^>]*>([^<]+)</dc:creator>", opf).group(1)
    assert creator == "[美] 爱德华·萨义德", "作者位只放作者:去掉「著」,也不带译者"

    assert "<dc:publisher>生活·读书·新知三联书店</dc:publisher>" in opf
    assert 'id="epub-contributor-trl"' in opf and "金钥" in opf
    assert 'property="role" scheme="marc:relators">trl' in opf
    assert "金钥" not in creator

    # CSS 里认 pandoc 的 epub3 脚注形态
    assert 'aside[epub|type~="footnote"]' in css


def test_book_css_styles_the_footnote_aside_the_way_pandoc_writes_it() -> None:
    css = BOOK_CSS.read_text(encoding="utf-8")
    assert 'aside[epub|type~="footnote"]' in css
    # 两套并存是故意的:不支持命名空间选择器的老阅读器要靠 class 规则兜底
    assert ".footnotes aside" in css


def test_add_contributor_is_idempotent(tmp_path: Path) -> None:
    """重复调用不会插出第二个译者(重转/后处理顺序变化都要安全)。"""
    from epub.pandoc import add_contributor

    epub = _stub_epub(tmp_path / "a.epub")
    assert add_contributor(epub, "金钥") is True
    assert add_contributor(epub, "金钥") is False

    with zipfile.ZipFile(epub) as z:
        opf = z.read("EPUB/content.opf").decode("utf-8")
        assert z.namelist()[0] == "mimetype", "重写后 mimetype 必须仍是第一项"
    assert opf.count('id="epub-contributor-trl"') == 1


def _stub_epub(path: Path) -> Path:
    """一个只有 mimetype + 最小 OPF 的 EPUB(pandoc 不在场也能测产物侧补丁)。"""
    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package version="3.0" xmlns="http://www.idpf.org/2007/opf" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        "  <metadata>\n"
        '    <dc:title id="t">书名</dc:title>\n'
        '    <dc:creator id="c">作者</dc:creator>\n'
        "  </metadata>\n"
        "</package>\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", "<container/>")
        z.writestr("EPUB/content.opf", opf)
    return path


@pytest.mark.parametrize("name", ["金钥", "A & B", "<译者>"])
def test_add_contributor_escapes_xml(tmp_path: Path, name: str) -> None:
    """译者名字里有 & < > 时不能写出不合法的 XML(一个 & 就能毁掉整份 OPF)。"""
    from epub.pandoc import add_contributor

    epub = _stub_epub(tmp_path / "b.epub")
    assert add_contributor(epub, name) is True
    with zipfile.ZipFile(epub) as z:
        opf = z.read("EPUB/content.opf").decode("utf-8")
    import xml.etree.ElementTree as ET

    ET.fromstring(opf)                      # 不良构会在这里炸
    assert "&amp;" in opf or "&" not in name
