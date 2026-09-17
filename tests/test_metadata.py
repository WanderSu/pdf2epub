"""封面与元数据测试(v0.3.2 P1-4)。

三件事:
1. 语言不再写死 —— 按正文脚本检测,`--lang` / 配置可覆盖;
2. `dc:identifier` 稳定 —— 同一本书重转必须拿到同一个 id(否则阅读器当新书,
   书架重复、阅读进度丢失),内容变了才换;
3. 封面 —— PDF 首页渲染成 JPEG 经 pandoc `--epub-cover-image` 注入,
   并且**不能**被内容校验当成「多出来的图」。

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
from conftest import make_pdf
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
    pdf = make_pdf(tmp_path / "测试书.pdf", pages=2, with_image=False)

    results = process_batch([pdf], config=dict(CFG), work_root=tmp_path / "work",
                            output_dir=tmp_path / "out", force=True)
    assert [r.status for r in results] == ["done"]
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


def test_cover_is_not_reported_as_extra_or_orphan_image(tmp_path: Path) -> None:
    """封面是 pandoc 注入的:内容对照不该把它算成「多出来的图」,也不算孤立图。"""
    pdf = make_pdf(tmp_path / "测试书.pdf", pages=1, with_image=False)

    results = process_batch([pdf], config=dict(CFG), work_root=tmp_path / "work",
                            output_dir=tmp_path / "out", force=True)

    result = verify_epub(results[0].epub)
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
    """封面渲染失败/没有 PDF 时不能阻断转换。"""
    pdf = make_pdf(tmp_path / "书.pdf", pages=1, with_image=False)

    cover = render_cover(pdf, tmp_path / "work")
    assert cover is not None and cover.exists() and cover.stat().st_size > 0
    assert not (tmp_path / "work" / "cover.jpg").with_suffix(".png").exists()

    # 传一个不存在的 PDF:返回 None 而不是抛异常
    assert render_cover(tmp_path / "不存在.pdf", tmp_path / "work") is None
