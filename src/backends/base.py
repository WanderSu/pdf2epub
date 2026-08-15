"""OCR/PDF 后端统一接口(idea.md §13)。

所有后端(本地 PyMuPDF4LLM、云端 MinerU、云端 PaddleOCR-VL)
必须实现统一的 convert() 接口,输出统一的 work/book.md + images/ 结构,
使后续 Markdown 清理 → Pandoc → EPUB 流程与具体后端解耦。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ConversionResult:
    """统一转换结果。"""

    book_md: Path          # work/book.md(Markdown 引用 images/ 相对路径)
    images_dir: Path       # work/images/
    backend: str           # 后端名: pymupdf / mineru / paddleocr
    task_id: str | None = None   # 云端任务 id(本地后端为 None)
    stats: dict = field(default_factory=dict)  # 统计信息(图片数、字符数等)


class Backend(ABC):
    """PDF → 统一 Markdown 后端接口。"""

    #: 后端标识名(用于配置切换)
    name: str = "base"

    @abstractmethod
    def convert(self, pdf_path: str | Path, work_dir: str | Path) -> ConversionResult:
        """将 PDF 转换为 work_dir/book.md + work_dir/images/。"""


def normalize_image_refs(md: str, images_abs: Path, images_dir: str = "images") -> str:
    """把 Markdown 中的图片引用前缀规范化为相对 images/。

    不同后端输出的引用前缀不同(绝对路径 / 相对路径 / 相对 cwd / 正反斜杠),
    统一替换为 "images/xxx"(相对 work_dir)。
    """
    import os

    prefixes = {
        str(images_abs),
        str(images_abs).replace("\\", "/"),
        str(images_abs.resolve()),
        str(images_abs.resolve()).replace("\\", "/"),
    }
    # 相对 cwd 变体(pymupdf4llm 会把 image_path 相对化为 cwd 形式)
    try:
        rel_cwd = os.path.relpath(images_abs, os.getcwd())
        prefixes.add(rel_cwd)
        prefixes.add(rel_cwd.replace("\\", "/"))
    except ValueError:
        pass
    for prefix in prefixes:
        md = md.replace(f"]({prefix}/", f"]({images_dir}/")
        md = md.replace(f'"]("{prefix}/', f'"]("{images_dir}/')  # 防御:带引号形式
    return md
