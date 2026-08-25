"""后端工厂:按配置名创建后端实例。

配置(idea.md §13)通过 ocr_backend 字段切换:
    ocr_backend = pymupdf | mineru | paddleocr
"""
from __future__ import annotations

from typing import Any

from .base import Backend, ConversionResult  # noqa: F401
from .mineru_backend import MinerUAdapter
from .paddleocr_backend import PaddleOCRAdapter
from .pymupdf_backend import PyMuPDFBackend

_BACKENDS: dict[str, type[Backend]] = {
    PyMuPDFBackend.name: PyMuPDFBackend,
    MinerUAdapter.name: MinerUAdapter,
    PaddleOCRAdapter.name: PaddleOCRAdapter,
}


def get_backend(name: str, config: dict[str, Any] | None = None) -> Backend:
    """创建后端实例。config 为对应后端的参数字典(不含凭证)。"""
    config = config or {}
    name = (name or "").lower().strip()
    if name == "pymupdf":
        return PyMuPDFBackend(
            write_images=config.get("write_images", True),
            bold_fonts=config.get("bold_fonts", []),
        )
    if name == "mineru":
        return MinerUAdapter(
            model_version=config.get("model_version", "vlm"),
            is_ocr=config.get("is_ocr", True),
            enable_formula=config.get("enable_formula", True),
            enable_table=config.get("enable_table", True),
            language=config.get("language", "ch"),
            max_pages_per_task=config.get("max_pages_per_task", 200),
        )
    if name == "paddleocr":
        return PaddleOCRAdapter(
            use_chart_recognition=config.get("use_chart_recognition", False),
            use_doc_orientation_classify=config.get("use_doc_orientation_classify", False),
            use_doc_unwarping=config.get("use_doc_unwarping", False),
        )
    raise ValueError(f"未知后端: {name!r}(可选: {list(_BACKENDS)})")
