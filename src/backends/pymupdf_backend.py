"""PyMuPDF4LLM 后端:电子版 PDF → 统一 Markdown + images/。

输入:具有可靠文字层的电子版 PDF
输出:work_dir/book.md + work_dir/images/(引用路径规范为相对 images/xxx.png)

关键参数(实测):
- write_images=True 必须显式开启,默认不提取图片
- image_path 指定图片输出目录;markdown 引用前缀需后处理为相对路径
"""
from __future__ import annotations

from pathlib import Path

import pymupdf4llm

from .base import Backend, ConversionResult, normalize_image_refs
from markdown.bold import annotate_bold

IMAGES_DIR = "images"


class PyMuPDFBackend(Backend):
    name = "pymupdf"

    def __init__(self, write_images: bool = True, bold_fonts: list[str] | None = None) -> None:
        self.write_images = write_images
        self.bold_fonts = bold_fonts or []

    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        """将电子版 PDF 转换为统一 Markdown + images/。"""
        pdf_path = Path(pdf_path)
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)

        images_abs = work_dir / IMAGES_DIR
        images_abs.mkdir(parents=True, exist_ok=True)

        md = pymupdf4llm.to_markdown(
            str(pdf_path),
            write_images=self.write_images,
            image_path=str(images_abs),
        )
        if self.bold_fonts:
            md = annotate_bold(md, pdf_path, extra_bold_fonts=self.bold_fonts)
        md = normalize_image_refs(md, images_abs)

        book_md = work_dir / "book.md"
        book_md.write_text(md, encoding="utf-8")
        img_count = len(list(images_abs.glob("*")))
        return ConversionResult(
            book_md=book_md,
            images_dir=images_abs,
            backend=self.name,
            stats={"chars": len(md), "images": img_count},
        )


# 兼容旧调用:convert_pdf_to_markdown(pdf, work) -> Path
def convert_pdf_to_markdown(
    pdf_path: str | Path,
    work_dir: str | Path,
    write_images: bool = True,
) -> Path:
    return PyMuPDFBackend(write_images=write_images).convert(pdf_path, work_dir).book_md
