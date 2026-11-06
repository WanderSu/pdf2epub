"""真实书籍端到端验收(EPUB 输出质量)。

自造小样本能证明规则「写对了」,证明不了「真实书籍转出来是好的」—— 这里拿仓库里
的真实样本跑**完整链路**(检测 → 提取 → 清理 → pandoc → 双层校验),验收:

  - 书名页只有一处:封面页确实是封面时用封面(排阅读顺序首位),否则用标题页;
  - 目录指向真实存在的章节、层级正确;
  - 图片 / 公式 / 表格 / 代码 / 脚注 / 硬换行(诗行)数量与源 Markdown 一致;
  - XHTML 全部是良构 XML(一个 `<br>` 就能毁掉整章);
  - CSS 嵌入且被正文引用;
  - 校验零失败零警告。

**只跑本地后端**(`--backend pymupdf`):扫描书在这里会被判成扫描版而调云端 OCR ——
慢、耗额度、无凭证的机器直接红。云端路径(OCR / hybrid)的验收见 `samples/pdfs/` 下
扫描/混合样本的手动跑法(docs/ 与 README 有说明),自动化测试不碰云端。

样本放在 `samples/pdfs/`(**已入库**)。早先指向 `books/`,而 `books/` 被 .gitignore
排除 —— 干净 clone 的机器上这 4 个测试会被 skip 掉且没有任何提示,等于验收形同虚设。
样本仍缺失时 skip,不 fail。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path

import pytest

import batch
from epub.content import verify_content
from epub.verify import verify_epub

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = PROJECT_ROOT / "samples" / "pdfs"
#: 真实样本:纯文字 PDF(仓库自带,1.2MB)
TEXT_BOOK = SAMPLES / "中文电子书测试.pdf"
#: 真实样本:双栏排版 PDF
TWO_COLUMN_BOOK = SAMPLES / "双栏测试.pdf"

CFG = {"pymupdf": {"write_images": True, "bold_fonts": []}}


def _convert(pdf: Path, tmp_path: Path):
    """本地链路转一本真实书,返回任务结果(backend_override 保证不碰云端)。"""
    results = batch.process_batch(
        [pdf], config=dict(CFG), backend_override="pymupdf",
        work_root=tmp_path / "work", output_dir=tmp_path / "out", force=True,
    )
    assert [r.status for r in results] == ["done"], [r.error for r in results]
    return results[0]


@pytest.mark.skipif(not TEXT_BOOK.exists(), reason=f"缺少真实样本 {TEXT_BOOK.name}")
def test_real_text_book_passes_both_verification_layers(tmp_path: Path) -> None:
    result = _convert(TEXT_BOOK, tmp_path)
    epub = result.epub
    work_md = tmp_path / "work" / batch.sanitize_name(TEXT_BOOK.stem) / "book.md"

    assert result.verify.startswith("0 失败 0 警告"), result.verify

    structure = verify_epub(epub)
    assert structure.ok
    assert structure.errors == []
    assert structure.warnings == [], [i.message for i in structure.warnings]
    assert structure.stats["cover"] == 0          # 首页是正文页 → 不硬当封面
    assert structure.stats["images"] >= 2         # 正文插图(没有封面图)
    assert structure.stats["toc_links"] >= 3      # 书名 + 三章
    assert structure.stats["css"] == 1

    content = verify_content(work_md, epub)
    assert content.issues == [], [i.message for i in content.issues]
    assert content.stats["content_epub_images"] == content.stats["content_md_images"] >= 2
    assert content.stats["content_epub_chars"] >= content.stats["content_md_chars"]


@pytest.mark.skipif(not TEXT_BOOK.exists(), reason=f"缺少真实样本 {TEXT_BOOK.name}")
def test_real_book_epub_is_structurally_valid(tmp_path: Path) -> None:
    """逐条验收 EPUB 本体:容器 / mimetype / manifest / spine / nav / XHTML 良构。"""
    import xml.etree.ElementTree as ET

    result = _convert(TEXT_BOOK, tmp_path)
    epub = result.epub

    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
        assert names[0] == "mimetype"                       # EPUB 规范要求放首位
        assert z.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in names
        for name in names:
            if name.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(z.read(name))                 # 不良构 XHTML 会在这里炸
        # 每个 manifest 条目都真实存在(引用资源不悬空)
        opf = z.read("EPUB/content.opf").decode("utf-8")
        for href in re.findall(r'<item [^>]*href="([^"]+)"', opf):
            assert f"EPUB/{href}" in names, href
    assert verify_epub(epub).stats["orphan_images"] == 0


@pytest.mark.skipif(not TEXT_BOOK.exists(), reason=f"缺少真实样本 {TEXT_BOOK.name}")
def test_real_book_without_a_cover_page_gets_a_title_page(tmp_path: Path) -> None:
    """这个样本首页就是正文(不是封面):不拿正文页冒充封面,改生成 pandoc 标题页。

    两条路都只允许**一处**书名页 —— 有封面用封面,没封面用标题页,不能既没封面
    又不给标题页(读者打开书看到一个"第1章")。
    """
    result = _convert(TEXT_BOOK, tmp_path)
    work = tmp_path / "work" / batch.sanitize_name(TEXT_BOOK.stem)

    assert not (work / "cover.jpg").exists()               # 没被硬当成封面

    with zipfile.ZipFile(result.epub) as z:
        names = z.namelist()
        opf = z.read("EPUB/content.opf").decode("utf-8")
        assert 'properties="cover-image"' not in opf        # manifest 里也没有封面项
        assert '<meta name="cover"' not in opf
        assert "EPUB/text/cover.xhtml" not in names
        assert "EPUB/text/title_page.xhtml" in names        # 标题页顶上了
        # 书名页里确实写着书名 + 作者(标题页不是空壳)
        title_page = z.read("EPUB/text/title_page.xhtml").decode("utf-8")
    title, author = batch.parse_title_author(TEXT_BOOK.stem)
    assert title in title_page
    if author:                                   # 文件名里没有作者时只校验书名
        assert author in title_page


@pytest.mark.skipif(not TWO_COLUMN_BOOK.exists(), reason=f"缺少真实样本 {TWO_COLUMN_BOOK.name}")
def test_real_two_column_book_keeps_chapters_and_headings(tmp_path: Path) -> None:
    result = _convert(TWO_COLUMN_BOOK, tmp_path)

    structure = verify_epub(result.epub)

    assert structure.ok and not structure.warnings
    assert structure.stats["toc_links"] >= 2                  # 双栏样本两节
    assert structure.stats["empty_chapters"] == 0
