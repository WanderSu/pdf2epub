"""封面与元数据测试(v0.3.2 P1-4)。

三件事:
1. 语言不再写死 —— 按正文脚本检测,`--lang` / 配置可覆盖;
2. `dc:identifier` 稳定 —— 同一本书重转必须拿到同一个 id(否则阅读器当新书,
   书架重复、阅读进度丢失),内容变了才换;
3. 封面 —— **第 1 页确实是封面**(整页图 / 只有几行短字段)时,原封面图渲成 JPEG
   经 pandoc `--epub-cover-image` 注入并排首位,**同时关掉标题页**(否则书名出现两遍);
   第 1 页是正文时不硬当封面,改回生成标题页。封面图也不能被内容校验当成
   「多出来的图」。

样本用真实本机链路(PyMuPDF 排版 PDF → 本地后端 → pandoc),不碰云端。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

import batch
import cli
from batch import _parse_pdf_date, process_batch, render_cover, source_date, stable_identifier
from conftest import make_pdf, make_pdf_with_cover
from epub.verify import verify_epub
from lang_detect import detect_language, resolve_language

CFG = {"pymupdf": {"write_images": True, "bold_fonts": []}}


def _opf(epub: Path) -> str:
    with zipfile.ZipFile(epub) as z:
        name = next(n for n in z.namelist() if n.endswith(".opf"))
        return z.read(name).decode("utf-8")


def _identifier_of(epub: Path) -> str:
    m = re.search(r"<dc:identifier[^>]*>([^<]+)</dc:identifier>", _opf(epub))
    assert m, "EPUB 缺少 dc:identifier"
    return m.group(1)


# ---------------------------------------------------------------- 语言检测

@pytest.mark.parametrize("text,expected", [
    ("这是一段中文测试文本,内容长度足够判断脚本构成,以句号结尾。", "zh-CN"),
    ("これはテストです。日本語の文章が続きます、句読点も含みます。", "ja"),
    ("이것은 한국어 테스트 문장입니다. 충분히 길게 작성합니다.", "ko"),
    ("This is an English test sentence, long enough to detect the script.", "en"),
    ("中文为主 mixed with some English words 仍然是中文书籍。", "zh-CN"),
    ("12 + 34 = 46", None),                      # 样本太少不下结论
    ("", None),
])
def test_detect_language(text: str, expected: str | None) -> None:
    assert detect_language(text) == expected


def test_resolve_language_override_wins() -> None:
    assert resolve_language("纯中文正文,应该被判为中文。", override="en") == "en"
    assert resolve_language("", override=None) == "zh-CN"          # 回退默认值
    assert resolve_language("This is English text, long enough.", override="") == "en"


def test_parse_pdf_date() -> None:
    assert _parse_pdf_date("D:20200101120000+08'00'") == "2020-01-01"
    assert _parse_pdf_date("D:19991231") == "1999-12-31"
    assert _parse_pdf_date("") is None
    assert _parse_pdf_date("junk") is None


# ---------------------------------------------------------------- 元数据接线

def test_stable_identifier_is_content_based(tmp_path: Path) -> None:
    pdf = make_pdf(tmp_path / "书.pdf", pages=1, with_image=False)

    first = stable_identifier(pdf, "书")
    assert first == stable_identifier(pdf, "书")            # 同一文件 → 同一 id
    assert first.startswith("urn:uuid:")
    assert stable_identifier(pdf, "另一个书名") != first     # 书名变了(id 也变)

    make_pdf(tmp_path / "另一本.pdf", pages=2, with_image=False)
    assert stable_identifier(tmp_path / "另一本.pdf", "书") != first   # 内容不同


def test_metadata_and_cover_end_up_in_the_epub(tmp_path: Path) -> None:
    # 第 1 页整页是封面图(scanned 书的常态):这样才会走「原封面作 cover.jpg」分支。
    # backend_override 固定本地后端 —— 封面页没有文字层,自动检测会把它算成扫描页
    # 而路由到云端 OCR(测试不碰云端)。
    pdf = make_pdf_with_cover(tmp_path / "测试书.pdf", text_pages=2)

    results = process_batch([pdf], config=dict(CFG), backend_override="pymupdf",
                            work_root=tmp_path / "work",
                            output_dir=tmp_path / "out", force=True)
    assert [r.status for r in results] == ["done"], [r.error for r in results]
    epub = results[0].epub
    opf = _opf(epub)

    assert stable_identifier(pdf, "测试书") in opf            # dc:identifier = 稳定 UUID
    assert "<dc:language>zh-CN</dc:language>" in opf
    assert f"<dc:date id=\"epub-date\">{source_date(pdf)}</dc:date>" in opf
    assert 'properties="cover-image"' in opf                 # 封面进了 manifest
    assert '<meta name="cover"' in opf                       # 阅读器据此显示缩略图
    with zipfile.ZipFile(epub) as z:
        cover_items = [n for n in z.namelist() if n.startswith("EPUB/media/")]
        assert cover_items and any(n.lower().endswith((".jpg", ".jpeg")) for n in cover_items)
    assert results[0].verify.startswith("0 失败")
    assert verify_epub(epub).ok


def test_cover_page_replaces_the_generated_title_page(tmp_path: Path) -> None:
    """有封面时不再生成标题页(否则读者会先看到两遍书名:标题页 + 封面)。

    两件事一起验:书名页只有**一处**(cover.xhtml),且它排在阅读顺序首位。
    """
    pdf = make_pdf_with_cover(tmp_path / "测试书.pdf", text_pages=2)

    epub = process_batch([pdf], config=dict(CFG), backend_override="pymupdf",
                         work_root=tmp_path / "work", output_dir=tmp_path / "out",
                         force=True)[0].epub
    opf = _opf(epub)

    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
    assert "EPUB/text/title_page.xhtml" not in names          # pandoc 标题页已关闭
    assert "EPUB/text/cover.xhtml" in names                   # 原封面页顶上了

    # 阅读顺序首位就是封面页(不是 nav,也不是第一章)
    ids = re.findall(r'<itemref idref="([^"]+)"', opf)
    hrefs = dict(re.findall(r'<item id="([^"]+)"[^>]*href="([^"]+)"', opf))
    assert hrefs.get(ids[0]) == "text/cover.xhtml", (ids[:2], hrefs)


def test_cover_is_not_reported_as_extra_or_orphan_image(tmp_path: Path) -> None:
    """封面图不算「多出来的图」,也不算孤立图(内容对照只比正文里的图)。"""
    pdf = make_pdf_with_cover(tmp_path / "测试书.pdf", text_pages=2)

    results = process_batch([pdf], config=dict(CFG), backend_override="pymupdf",
                            work_root=tmp_path / "work",
                            output_dir=tmp_path / "out", force=True)

    result = verify_epub(results[0].epub)
    assert result.stats["cover"] == 1
    assert result.stats["orphan_images"] == 0
    assert result.stats["images_no_cover"] == 0
    assert result.stats["images"] == 1
    assert not [i for i in result.issues if i.code.startswith(("content_image", "image_orphan"))]
    assert results[0].verify.startswith("0 失败")


def test_reconversion_keeps_the_same_identifier(tmp_path: Path) -> None:
    """重转必须保持同一个 dc:identifier(否则阅读器当新书:书架重复、进度丢失)。

    注意不能整份 OPF 逐字节比:pandoc 会写 dcterms:modified 时间戳,那本来就该变。
    """
    pdf = make_pdf(tmp_path / "重转书.pdf", pages=1, with_image=False)
    kwargs = dict(config=dict(CFG), work_root=tmp_path / "work", output_dir=tmp_path / "out")

    id_first = _identifier_of(process_batch([pdf], force=True, **kwargs)[0].epub)
    id_second = _identifier_of(process_batch([pdf], force=True, **kwargs)[0].epub)

    assert id_first == id_second
    assert id_first == stable_identifier(pdf, "重转书")


def test_language_of_english_book_is_detected(tmp_path: Path) -> None:
    md = tmp_path / "english.md"
    md.write_text("# An English Book\n\nThis is a fairly long English paragraph, used to "
                  "detect the script of the book, and it keeps going for a while.\n",
                  encoding="utf-8")

    results = process_batch([md], config=dict(CFG), work_root=tmp_path / "work",
                            output_dir=tmp_path / "out", force=True)

    assert "<dc:language>en</dc:language>" in _opf(results[0].epub)


def test_cli_lang_overrides_detection(tmp_path: Path) -> None:
    md = tmp_path / "中文书.md"
    md.write_text("# 中文书\n\n这是一段足够长的中文正文,用来触发脚本检测,并以句号结尾。\n",
                  encoding="utf-8")
    out, work = tmp_path / "out", tmp_path / "work"

    code = cli.main([str(md), "-o", str(out), "--work", str(work),
                     "--lang", "ja", "--force", "--no-log"])

    assert code == 0
    opf = _opf(out / "中文书.epub")
    assert "<dc:language>ja</dc:language>" in opf          # CLI 覆盖 > 正文检测


def test_cover_rendering_is_optional(tmp_path: Path) -> None:
    """底层渲染只负责「把这一页画成 JPEG」;失败必须返回 None,不能抛异常打断转换。

    「这一页该不该当封面」不在这里判(见 ``cover.detect_cover``),所以这里给一张
    普通文字页也照样渲出来 —— 决定权在 ``_process_pdf``。
    """
    pdf = make_pdf(tmp_path / "书.pdf", pages=1, with_image=False)

    cover = render_cover(pdf, tmp_path / "work")
    assert cover is not None and cover.exists() and cover.stat().st_size > 0
    assert not (tmp_path / "work" / "cover.jpg").with_suffix(".png").exists()

    # 传一个不存在的 PDF:返回 None 而不是抛异常
    assert render_cover(tmp_path / "不存在.pdf", tmp_path / "work") is None
