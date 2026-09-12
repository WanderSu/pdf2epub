"""pytest 共享夹具:样本现场生成,不依赖仓库里的真实书籍。

测试原则(计划 1.2):任何一台空样本目录的机器,`uv run pytest -q` 都应全绿。
因此:
  - PDF 样本用 pymupdf 现场生成(见 make_sample_pdf)
  - EPUB 样本用项目真实工具链(pandoc + config/book.css)现场生成,而非手工伪造
需要的系统工具缺失时 skip(不 fail),避免环境差异导致假红。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

BOOK_CSS = PROJECT_ROOT / "config" / "book.css"


def write_png(path: Path, size: int = 16) -> Path:
    """写一张合法的小 PNG(PyMuPDF 渲染),供 PDF/EPUB 图片引用测试使用。"""
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size))
    pix.set_rect(pix.irect, (200, 60, 0))
    pix.save(path)
    return path


def _pandoc() -> str:
    """pandoc 可执行文件;缺失则 skip(避免环境差异造成假红)。"""
    exe = shutil.which("pandoc")
    if exe is None:
        pytest.skip("未安装 pandoc(EPUB 生成依赖),跳过需要它的测试")
    return exe


def build_epub(
    work_dir: Path,
    out_path: Path,
    *,
    title: str = "测试书籍",
    body: str | None = None,
    with_image: bool = True,
    with_math: bool = True,
    extra_pandoc_args: list[str] | None = None,
) -> Path:
    """用 pandoc 生成一个真实结构的 EPUB(与生产的转换路径一致)。"""
    work_dir.mkdir(parents=True, exist_ok=True)
    parts = [f"# {title}", ""]
    if body is None:
        body = ("这是用于结构校验的测试正文段落,内容足够长以避免被判为空章节。\n\n"
                "第二段:中文排版与 ASCII 混排 PDF、EPUB、Python。")
    parts += [body, ""]
    if with_image:
        write_png(work_dir / "img1.png")
        parts += ["![测试插图](img1.png)", ""]
    if with_math:
        parts += ["质能方程 $E = mc^2$ 是行内公式。", "",
                  "$$", "\\int_0^1 x^2 \\, dx = \\frac{1}{3}", "$$", ""]
    md = work_dir / "book.md"
    md.write_text("\n".join(parts), encoding="utf-8")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_pandoc(), str(md), "-o", str(out_path),
           "--toc", "--toc-depth=3", "--mathml",
           "--resource-path", str(work_dir),
           "--metadata", f"title={title}",
           "--metadata", "lang=zh-CN"]
    if BOOK_CSS.exists():
        cmd += ["--css", str(BOOK_CSS)]
    cmd += extra_pandoc_args or []
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        pytest.fail(f"pandoc 生成样本 EPUB 失败: {proc.stderr[:500]}")
    return out_path


def rewrite_epub(src: Path, dst: Path, *, drop: set[str] | None = None,
                 drop_suffixes: tuple[str, ...] = ()) -> Path:
    """复制 EPUB,可剔除指定条目(用于制造「图片丢失」「容器缺失」等损坏样本)。"""
    import zipfile

    drop = drop or set()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename in drop or item.filename.endswith(drop_suffixes):
                continue
            zout.writestr(item, zin.read(item.filename))
    return dst


@pytest.fixture(scope="session")
def sample_epub(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """一份结构完整的 EPUB(含图片 + MathML 公式)。"""
    base = tmp_path_factory.mktemp("epub_sample")
    return build_epub(base / "src", base / "ok.epub", title="样本书")


def make_pdf(path: Path, *, pages: int = 2, with_image: bool = True) -> Path:
    """现场生成一个小 PDF(中文正文 + 可选图片),不依赖仓库里的样本文件。

    用 PyMuPDF 直接排版,因此转换链路(text 检测 → PyMuPDF4LLM 提取)可离线跑通。

    **每页正文必须远超检测阈值(有效字符 ≥50)**:低于阈值会被判成扫描版,于是
    本地链路的测试会去调云端 OCR(慢 + 消耗额度)。每页文字带页码,既加长长度,
    也避免同一段落在多页重复而被清理器当成页眉页脚剔除。
    """
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    img = write_png(path.parent / "fig.png") if with_image else None
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_textbox(pymupdf.Rect(60, 60, 535, 100),
                            f"第 {i + 1} 章 测试标题", fontname="china-s", fontsize=18)
        page.insert_textbox(
            pymupdf.Rect(60, 110, 535, 300),
            f"这是第 {i + 1} 页用于管线测试的正文段落,内容长度足以被判定为文字页,"
            f"并以句号结尾;再补一句保证有效字符数稳定超过检测阈值。\n"
            f"第二段同样保持足够长度,避免样本被判成扫描版 —— 一旦被判扫描版,"
            f"本地链路的测试就会去调云端 OCR,既慢又消耗额度。",
            fontname="china-s", fontsize=11, lineheight=1.6,
        )
        if img is not None and i == 0:
            page.insert_image(pymupdf.Rect(60, 320, 260, 470), filename=str(img))
    doc.save(path)
    doc.close()
    return path


def make_fake_text_layer_pdf(path: Path, *, text_pages: int = 2,
                             junk_pages: int = 1) -> Path:
    """生成「部分页文字层损坏(伪文字层)」的 PDF,供检测器 golden 样本使用。

    真实伪文字层是嵌入字体没有正确 ToUnicode → 提取出私有区乱码;但 PyMuPDF
    **写不出未映射码位**(会退化成 `?` 或被丢弃),所以这里用 CJK 扩展 A 字符模拟:
    它落在检测器的 SUSPICIOUS_RE 里、正常正文几乎不出现,走的是同一条判定路径
    (页内可疑字符占比 > 阈值 → 视为伪文字层页,不算文字页)。
    前 text_pages 页是正常文字页,其余 junk_pages 页是乱码页。
    """
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    junk = "".join(chr(0x3400 + i) for i in range(10))     # 㐀㐁㐂… 每行 10 字

    doc = pymupdf.open()
    for i in range(text_pages + junk_pages):
        page = doc.new_page(width=595, height=842)
        if i < text_pages:
            page.insert_textbox(
                pymupdf.Rect(60, 60, 535, 260),
                f"第 {i + 1} 页正常文字层,内容长度足以被判定为文字页,并以句号结尾。"
                f"再补一句保证有效字符数稳定超过检测阈值(每页 50 字),"
                f"避免这个样本被判成扫描版而跑去调云端 OCR。",
                fontname="china-s", fontsize=12, lineheight=1.6,
            )
            continue
        for line in range(8):
            page.insert_textbox(
                pymupdf.Rect(60, 60 + line * 40, 535, 100 + line * 40),
                junk, fontname="china-s", fontsize=14,
            )
    doc.save(path)
    doc.close()
    return path


def make_paged_pdf(path: Path, pages: int) -> Path:
    """生成 N 页的小 PDF(每页一行短文字),用于分片/页数相关用例。

    只关心页数,不关心内容,所以刻意做到体积很小(几百页也只有几百 KB),
    不必为「450 页扫描书」造一个真的大文件。
    """
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 80), f"page {i + 1}", fontsize=12)
    doc.save(path, deflate=True, garbage=3)
    doc.close()
    return path


def make_mixed_pdf(path: Path, layout: str = "TTTSSTTT") -> Path:
    """按 layout 生成混合 PDF:T = 文字页,S = 扫描页(纯图,无文字层)。

    layout 顺序对应页序,例如 "TTTSSTTT" = 前 3 页文字、第 4-5 页扫描、后 3 页文字。
    文字页带可识别页码的文字("第 N 页正文"之类),便于断言页序;
    扫描页只放一张图,检测器据此判定为扫描页(hybrid 路由会把它送 OCR)。
    """
    import pymupdf

    path.parent.mkdir(parents=True, exist_ok=True)
    scan_img = write_png(path.parent / "scan_page.png", size=64)

    doc = pymupdf.open()
    for i, kind in enumerate(layout, start=1):
        page = doc.new_page(width=595, height=842)
        if kind.upper() == "S":
            page.insert_image(page.rect, filename=str(scan_img))
            continue
        page.insert_textbox(pymupdf.Rect(60, 60, 535, 120),
                            f"第 {i} 章 测试标题", fontname="china-s", fontsize=18)
        page.insert_textbox(
            pymupdf.Rect(60, 130, 535, 300),
            f"这是原始 PDF 第 {i} 页的正文段落,内容长度足以被判定为文字页,"
            f"并以句号结尾;再补一句话确保字符数稳定超过检测阈值(每页 50 字),"
            f"以免这个样本被判成扫描版而跑去调云端 OCR。",
            fontname="china-s", fontsize=11, lineheight=1.6,
        )
    doc.save(path)
    doc.close()
    return path
