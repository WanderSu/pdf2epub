"""pytest 共享夹具:样本现场生成,不依赖仓库里的真实书籍。

测试原则(计划 1.2):任何一台空样本目录的机器,`uv run pytest -q` 都应全绿。
因此:
  - PDF 样本用 pymupdf 现场生成(见 make_sample_pdf)
  - EPUB 样本用项目真实工具链(pandoc + config/book.css)现场生成,而非手工伪造
需要的系统工具缺失时 skip(不 fail),避免环境差异导致假红。
"""
from __future__ import annotations

import base64
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

# 8x8 纯色 PNG(手工拼字节,避免依赖 Pillow)
_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAIAQMAAAD+wSzIAAAABlBMVEX///+/v7+jQ3Y5AAAADklEQVQI12P4AIX8"
    "EBlbAN9WDeYAAAAASUVORK5CYII="
)


def _pandoc() -> str:
    exe = shutil.which("pandoc")
    if exe is None:
        pytest.skip("未安装 pandoc(EPUB 生成依赖),跳过需要它的测试")
    return exe


def write_png(path: Path) -> Path:
    """写一张可用的最小 PNG(8x8),供图片引用测试使用。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_PNG_1PX)
    return path


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
