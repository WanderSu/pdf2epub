"""原始 HTML 空元素与 XHTML 良构性(`<br>` → `<br />`)。

被钉住的缺陷:EPUB 的正文是 **XHTML(XML)**,`<br>` 是未闭合标签;而 pandoc 对
Markdown 里的原始 HTML **原样透传**(云端 OCR 的图片文字块就写 `<br>`)。实测一本
300KB 的章节只因为这**一个**标签,整份 XHTML 不再是良构 XML —— Readium / KOReader /
epubcheck 会拒绝或错乱渲染,而旧的结构校验(纯正则)完全看不见。

修复分两层,这里两层都测:
  - 生成侧:`build_epub` 归一化给 pandoc 的副本(`<br>` → `<br />`),不就地改 book.md;
  - 校验侧:`verify_epub` 用 XML 解析每份 XHTML,不良构判 error(见 test_epub_verify.py)。
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from conftest import build_epub as pandoc_epub
from epub.pandoc import build_epub, normalize_xhtml_voids
from epub.verify import verify_epub

#: 实测形态:云端 OCR 的「图片文字」块,出版社名后跟着换行标记与注释
OCR_PICTURE_TEXT = (
    "# 样本书\n\n"
    "<!-- Start of picture text -->\n"
    "中國文化傳播出版社<br><!-- End of picture text -->一切坚固的东西都烟消云散了。\n"
)


# ---------------------------------------------------------------- 归一化

def test_bare_br_becomes_self_closing() -> None:
    assert normalize_xhtml_voids("第一行<br>第二行") == "第一行<br />第二行"


def test_already_closed_br_is_unchanged() -> None:
    src = "第一行<br />第二行<br/>第三行"
    assert normalize_xhtml_voids(src) == "第一行<br />第二行<br />第三行"


def test_void_tags_with_attributes_keep_them() -> None:
    src = '<img src="images/a.png" alt="图">'
    assert normalize_xhtml_voids(src) == '<img src="images/a.png" alt="图" />'


def test_other_void_tags_and_case() -> None:
    src = "<HR>\n<IMG SRC=\"a.png\">\n文本"
    assert normalize_xhtml_voids(src) == "<HR />\n<IMG SRC=\"a.png\" />\n文本"


def test_non_void_tags_are_untouched() -> None:
    """`<div>`/`<span>` 不是空元素,补斜杠会破坏结构(必须原样保留)。"""
    src = "<div class=\"x\">正文</div><span>字</span>"
    assert normalize_xhtml_voids(src) == src


def test_code_fence_is_untouched() -> None:
    """书里讲 HTML 的代码块不能被改写。"""
    src = "```html\n第一行<br>第二行\n```\n"
    assert normalize_xhtml_voids(src) == src


# ---------------------------------------------------------------- 端到端

def _xhtml_docs(epub: Path) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(epub) as z:
        return [(n, z.read(n)) for n in z.namelist() if n.endswith(".xhtml")]


def test_epub_from_ocr_style_markdown_is_well_formed(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text(OCR_PICTURE_TEXT, encoding="utf-8")

    epub = build_epub(md, work, tmp_path / "out", title="样本书")

    for name, data in _xhtml_docs(epub):
        ET.fromstring(data)                       # 不良构会在这里抛 ParseError
    assert verify_epub(epub).ok
    # 换行仍然存在(归一化只改写法,不改语义)
    chapter = next(d for n, d in _xhtml_docs(epub) if "<br" in d.decode("utf-8"))
    assert "<br />" in chapter.decode("utf-8")


def test_pandoc_alone_leaves_bare_br_and_verifier_catches_it(tmp_path: Path) -> None:
    """反向验证:pandoc 自己拿到 `<br>` 会写出不良构 XHTML —— 校验必须报出来。"""
    src = tmp_path / "src"
    body = ("<!-- Start of picture text -->\n"
            "中國文化傳播出版社<br><!-- End of picture text -->一切坚固的东西都烟消云散了。\n")
    epub = pandoc_epub(src, tmp_path / "raw.epub", title="样本书", body=body,
                       with_image=False, with_math=False)

    result = verify_epub(epub)

    assert any(i.code == "xhtml_invalid" for i in result.errors)
    assert not result.ok


def test_source_markdown_is_not_rewritten(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text(OCR_PICTURE_TEXT, encoding="utf-8")

    build_epub(md, work, tmp_path / "out", title="样本书")

    assert md.read_text(encoding="utf-8") == OCR_PICTURE_TEXT
    copy = work / "book.pandoc.md"
    assert copy.exists() and "<br>" not in copy.read_text(encoding="utf-8")


def test_no_raw_html_means_no_copy(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("# 无原始 HTML\n\n只有正文,句号结尾。\n", encoding="utf-8")

    build_epub(md, work, tmp_path / "out", title="无原始 HTML")

    assert not (work / "book.pandoc.md").exists()


# ---------------------------------------------------------------- 产物自检

def test_missing_output_is_a_hard_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """pandoc 退出码 0 却没写出文件 → 必须报错,不能把「没有产物」当成功。"""
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("# 样本书\n\n正文,句号结尾。\n", encoding="utf-8")

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("epub.pandoc.subprocess.run", lambda *a, **k: FakeProc())

    with pytest.raises(RuntimeError, match="没有写出 EPUB"):
        build_epub(md, work, tmp_path / "out", title="样本书")


def test_empty_output_file_is_a_hard_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("# 样本书\n\n正文,句号结尾。\n", encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"")     # pandoc 写出 0 字节产物
        return FakeProc()

    monkeypatch.setattr("epub.pandoc.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="产物缺失或为空"):
        build_epub(md, work, out, title="样本书")


def test_pandoc_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("# 样本书\n\n正文,句号结尾。\n", encoding="utf-8")

    class FakeProc:
        returncode = 43
        stdout = ""
        stderr = "pandoc: 出错了"

    monkeypatch.setattr("epub.pandoc.subprocess.run", lambda *a, **k: FakeProc())

    with pytest.raises(RuntimeError, match="Pandoc 失败"):
        build_epub(md, work, tmp_path / "out", title="样本书")


def test_subprocess_file_not_found_is_environment_error(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """which() 能解析、真正执行却找不到(Windows [WinError 2])→ 环境缺失,不该重试。"""
    from epub.pandoc import PandocMissingError

    work = tmp_path / "work"
    work.mkdir()
    md = work / "book.md"
    md.write_text("# 样本书\n\n正文,句号结尾。\n", encoding="utf-8")

    def boom(*a, **k):
        raise FileNotFoundError(2, "系统找不到指定的文件。")

    monkeypatch.setattr("epub.pandoc.subprocess.run", boom)

    with pytest.raises(PandocMissingError):
        build_epub(md, work, tmp_path / "out", title="样本书")
